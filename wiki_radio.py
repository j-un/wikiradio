#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
wiki_radio.py — Wikipediaのランダム記事をネタにAIラジオを延々流すCLI。

パイプライン:
    ランダム記事取得 → LLM (Claude / Ollama) で台本生成 → VOICEVOXで音声化 → 再生
    再生中に裏で次のエピソードを先読み生成（concurrent.futures）。

前提:
- VOICEVOX Engine が http://localhost:50021 で起動済み
    docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest
- Claude利用時: claude CLI がインストール済み・ログイン済み
- Ollama利用時: ollama serve が起動済み
- 再生コマンド: macOS は afplay、それ以外は ffplay を自動選択。

実行:
    uv run wiki_radio.py                                   # Claude haiku (既定)
    uv run wiki_radio.py --llm ollama                      # Ollama (gemma4:latest)
    uv run wiki_radio.py --llm ollama --model llama3.2:3b
    uv run wiki_radio.py --model claude-sonnet-4-6
    uv run wiki_radio.py --max-budget-usd 0.02
    uv run wiki_radio.py --music-dir ~/Music/radio
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import httpx

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

WIKI_API = "https://ja.wikipedia.org/w/api.php"
VOICEVOX = os.environ.get("VOICEVOX_URL", "http://localhost:50021")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:latest")
DEFAULT_MAX_BUDGET_USD = float(os.environ.get("CLAUDE_MAX_BUDGET_USD", "0.05"))
DEFAULT_SPEED_SCALE = float(os.environ.get("VOICEVOX_SPEED", "1.1"))

MIN_BODY_CHARS = 1500
MAX_BODY_CHARS = 4000
EPISODE_MAX_RETRIES = 3
MUSIC_RECENT_LIMIT = 5  # 直近N曲は再選択しない

USER_AGENT = "wiki-radio/0.1 (https://github.com/local/wikiradio; bot) httpx/0.27"

# VOICEVOX の話者ID。`curl localhost:50021/speakers` で一覧確認可。
SPEAKER_A = int(os.environ.get("SPEAKER_A", "20"))  # ナビ役
SPEAKER_B = int(os.environ.get("SPEAKER_B", "2"))  # 相方

MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".aiff", ".aif", ".ogg"}

# 番組スタイルのプロンプト。ここを書き換えるのが一番効く。
PROMPT_DUO = """\
あなたは深夜ラジオ番組の構成作家です。
番組は深夜に延々と続いており、これはその中の1コーナーです。
以下のWikipedia記事を題材に、パーソナリティ2人(AとB)の自然な掛け合いトーク台本を作ってください。

キャラクター:
- A: 進行役。落ち着いた口調で話を引き出す
- B: 好奇心旺盛でよく食いつく。ツッコミ担当

構成の要件:
- 単なる要約の読み上げにせず、雑談・脱線・ツッコミを交えた会話にする
- 冒頭は「番組の始まり」ではなく「次のコーナーへの自然な移行」にする
    例: 「さて、次はこんなテーマ」「続いてはちょっとマニアックな話で」
    禁止: 「こんばんは」「今日の番組は」「さあ始まりました」などの番組開始フレーズ
- 本編は雑談・脱線・ツッコミを交えた自然な会話にする（要約の棒読みにしない）
- 締めはコーナーの終わりとして軽くまとめる。番組全体の終了感は出さない
    禁止: 「また来週」「お聴きいただきありがとうございました」などの番組終了フレーズ
- 全体で150〜300秒程度
- 専門用語は会話の中でさりげなく噛み砕く
- 出力は **JSON配列のみ**。前後に説明やマークダウンを付けない
- 各要素は {{"speaker": "A" または "B", "text": "セリフ"}}

記事タイトル: {title}
記事本文(抜粋):
{body}
"""

PROMPT_SOLO = """\
あなたは深夜ラジオ番組のDJ兼構成作家です。
番組は深夜に延々と続いており、これはその中の1コーナーです。
以下のWikipedia記事を題材に、DJ一人語りのトーク台本を作ってください。

構成の要件:
- 単なる要約ではなく、語りかけ・脱線・個人的な感想を交えた一人語りにする
- 冒頭は「番組の始まり」ではなく「次の話題への自然な移行」にする
    例: 「さて、次はこんなテーマ」「続いてはちょっとマニアックな話で」
    禁止: 「こんばんは」「今日の番組は」「さあ始まりました」などの番組開始フレーズ
- 本編は語りかけ・脱線・個人的な感想を交えた一人語りにする（要約の棒読みにしない）
- 締めはコーナーの終わりとして軽くまとめる。番組全体の終了感は出さない
    禁止: 「また来週」「お聴きいただきありがとうございました」などの番組終了フレーズ
- 全体で150〜300秒程度
- 専門用語はさりげなく噛み砕く
- 出力は **JSON配列のみ**。前後に説明やマークダウンを付けない
- 各要素は {{"speaker": "A", "text": "セリフ"}}

記事タイトル: {title}
記事本文(抜粋):
{body}
"""


@dataclass
class Episode:
    title: str
    segments: list[dict]  # [{"speaker": "A"/"B", "text": ...}]
    wav_path: str


@dataclass
class MusicInterlude:
    intro_wav: str  # 事前合成済みイントロwavのパス
    music_path: str  # 音楽ファイルパス
    outro_text: str  # アウトロのテキスト（音楽後に合成）
    display: str  # ログ表示用


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _log(msg: str) -> None:
    stripped = msg.lstrip("\n")
    leading = "\n" * (len(msg) - len(stripped))
    print(f"{leading}[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {stripped}")


# ---------------------------------------------------------------------------
# 1. Wikipedia ランダム記事取得
# ---------------------------------------------------------------------------


def fetch_random_article(client: httpx.Client) -> tuple[str, str]:
    """本文長の足切りを満たすランダム記事 (title, body) を返す。"""
    for _ in range(20):
        r = client.get(
            WIKI_API,
            params={
                "action": "query",
                "list": "random",
                "rnnamespace": 0,
                "rnlimit": 1,
                "format": "json",
            },
        )
        r.raise_for_status()
        title = r.json()["query"]["random"][0]["title"]

        r = client.get(
            WIKI_API,
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "exsectionformat": "plain",
                "titles": title,
                "format": "json",
            },
        )
        r.raise_for_status()
        pages = r.json()["query"]["pages"]
        body = next(iter(pages.values())).get("extract", "")
        if len(body) >= MIN_BODY_CHARS:
            return title, body[:MAX_BODY_CHARS]
    raise RuntimeError("十分な長さの記事を引けませんでした")


# ---------------------------------------------------------------------------
# 2. LLM で台本生成
# ---------------------------------------------------------------------------


def generate_script(
    client: httpx.Client,
    title: str,
    body: str,
    style: str,
    llm: str,
    model: str,
    max_budget_usd: float,
) -> list[dict]:
    prompt = (PROMPT_SOLO if style == "solo" else PROMPT_DUO).format(
        title=title, body=body
    )
    if llm == "ollama":
        raw = _call_ollama(client, prompt, model)
    else:
        raw = _call_claude(prompt, model, max_budget_usd)
    return _parse_segments(raw, style)


def _call_ollama(client: httpx.Client, prompt: str, model: str) -> str:
    r = client.post(
        f"{OLLAMA}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.8},
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["response"]


def _call_claude(prompt: str, model: str, max_budget_usd: float) -> str:
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--max-budget-usd",
        str(max_budget_usd),
        "--output-format",
        "json",
        "--tools",
        "",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Claude CLI エラー (exit {e.returncode}): {e.stderr.strip()}"
        ) from e
    data = json.loads(result.stdout)
    cost = data.get("total_cost_usd", 0.0)
    _log(f"  Claude費用: ${cost:.5f}")
    return data["result"].strip()


def _escape_json_control_chars(s: str) -> str:
    """JSON文字列値内の未エスケープ制御文字をエスケープする。"""
    result = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ord(ch) < 0x20:
            result.append(
                {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, f"\\u{ord(ch):04x}")
            )
        else:
            result.append(ch)
    return "".join(result)


def _parse_segments(raw: str, style: str) -> list[dict]:
    """LLM出力からJSON配列を取り出す。"""
    s = raw.strip()
    # コードフェンス (```json ... ``` 形式) を正規表現で除去
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    else:
        start, end = s.find("["), s.rfind("]")
        if start != -1 and end != -1:
            s = s[start : end + 1]
    try:
        segs = json.loads(s)
    except json.JSONDecodeError:
        # LLMがJSON文字列内に生の制御文字を埋め込んだ場合のフォールバック
        try:
            segs = json.loads(_escape_json_control_chars(s))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"台本のJSONパースに失敗: {e}\n--- 生出力 (先頭300文字) ---\n{raw[:300]}"
            ) from e
    # speaker を A/B に正規化（solo は全部 A）
    out = []
    for seg in segs:
        spk = (
            "A"
            if style == "solo"
            else ("B" if str(seg.get("speaker", "A")).upper().startswith("B") else "A")
        )
        text = str(seg.get("text", "")).strip()
        if text:
            out.append({"speaker": spk, "text": text})
    return out


# ---------------------------------------------------------------------------
# 3. VOICEVOX で音声化 → wav 結合
# ---------------------------------------------------------------------------


def synth_segment(
    client: httpx.Client, text: str, speaker: int, speed: float = 1.0
) -> bytes:
    q = client.post(
        f"{VOICEVOX}/audio_query", params={"text": text, "speaker": speaker}, timeout=60
    )
    q.raise_for_status()
    query = q.json()
    query["speedScale"] = speed
    syn = client.post(
        f"{VOICEVOX}/synthesis", params={"speaker": speaker}, json=query, timeout=120
    )
    syn.raise_for_status()
    return syn.content


def build_episode_wav(
    client: httpx.Client, segments: list[dict], speed: float = 1.0
) -> str:
    """全セグメントを並列合成して1つのwavに結合し、パスを返す。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [
            ex.submit(
                synth_segment,
                client,
                seg["text"],
                SPEAKER_A if seg["speaker"] == "A" else SPEAKER_B,
                speed,
            )
            for seg in segments
        ]
        parts = [f.result() for f in futures]

    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    out.close()
    try:
        _concat_wav(parts, out.name)
    except Exception:
        _safe_unlink(out.name)
        raise
    return out.name


def _concat_wav(wav_blobs: list[bytes], dest: str) -> None:
    """同一フォーマット前提でwavを連結。間に短い無音を挟む。"""
    if not wav_blobs:
        return
    with wave.open(dest, "wb") as w:
        params = None
        silence = b""
        for blob in wav_blobs:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_name = tmp.name
            try:
                tmp.write(blob)
                tmp.close()
                with wave.open(tmp_name, "rb") as r:
                    if params is None:
                        params = r.getparams()
                        w.setparams(params)
                        silence = b"\x00" * (
                            params.sampwidth
                            * params.nchannels
                            * int(params.framerate * 0.4)
                        )
                    w.writeframes(r.readframes(r.getnframes()))
                    w.writeframes(silence)
            finally:
                _safe_unlink(tmp_name)


# ---------------------------------------------------------------------------
# 4. 再生
# ---------------------------------------------------------------------------


def play(wav_path: str) -> None:
    if shutil.which("afplay"):
        subprocess.run(["afplay", wav_path], check=False)
    elif shutil.which("ffplay"):
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav_path],
            check=False,
        )
    else:
        _log(f"[再生コマンドが見つかりません] 手動再生: {wav_path}")
        input("Enterで次へ...")


# ---------------------------------------------------------------------------
# 5. 音楽インタールード
# ---------------------------------------------------------------------------


def load_music_cache(music_dir: str) -> list[str]:
    """起動時に音楽ファイル一覧をキャッシュして返す。"""
    return [
        os.path.join(root, f)
        for root, _, filenames in os.walk(music_dir)
        for f in filenames
        if os.path.splitext(f)[1].lower() in MUSIC_EXTS
    ]


def _parse_music_info(path: str) -> tuple[str, str]:
    """ファイル名から (アーティスト, 曲名) を返す。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d+[\.\s]+", "", stem)  # 先頭のトラック番号を除去
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem.strip()


def _pick_random_music(music_files: list[str], recent: deque[str]) -> str | None:
    """直近MUSIC_RECENT_LIMIT曲を除外してランダム選択。"""
    available = [f for f in music_files if f not in recent]
    if not available:
        recent.clear()
        available = music_files
    return random.choice(available) if available else None


def _prepare_music_interlude(
    music_files: list[str], speed: float, recent: deque[str]
) -> MusicInterlude | None:
    """エピソード再生中にバックグラウンドでイントロを事前合成する。"""
    path = _pick_random_music(music_files, recent)
    if path is None:
        return None

    recent.append(path)
    artist, title = _parse_music_info(path)
    display = f"{artist} - {title}" if artist else title
    intro_text = (
        f"続いては、{artist}の{title}をお聴きください。"
        if artist
        else f"続いては、{title}をお聴きください。"
    )
    outro_text = f"{artist}で{title}でした。" if artist else f"{title}でした。"

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as c:
        wav_bytes = synth_segment(c, intro_text, SPEAKER_A, speed)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_name = tmp.name
    try:
        tmp.write(wav_bytes)
        tmp.close()
    except Exception:
        _safe_unlink(tmp_name)
        raise

    return MusicInterlude(
        intro_wav=tmp_name,
        music_path=path,
        outro_text=outro_text,
        display=display,
    )


def play_prepared_interlude(interlude: MusicInterlude, speed: float) -> None:
    """事前合成済みのインタールードを再生する。"""
    _log(f"\n🎵 音楽: {interlude.display}")
    try:
        play(interlude.intro_wav)
    finally:
        _safe_unlink(interlude.intro_wav)

    play(interlude.music_path)

    # アウトロは音楽終了後に合成（次エピソードの先読み待機中に実行されるためラグは許容範囲）
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as c:
        wav_bytes = synth_segment(c, interlude.outro_text, SPEAKER_A, speed)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_name = tmp.name
    try:
        tmp.write(wav_bytes)
        tmp.close()
        play(tmp_name)
    finally:
        _safe_unlink(tmp_name)


# ---------------------------------------------------------------------------
# 6. エピソード生成
# ---------------------------------------------------------------------------


def make_episode(
    style: str, llm: str, model: str, max_budget_usd: float, speed: float = 1.0
) -> Episode:
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as c:
        title, body = fetch_random_article(c)
        _log(f"  お題: {title}")
        segs = generate_script(c, title, body, style, llm, model, max_budget_usd)
        wav = build_episode_wav(c, segs, speed)
        return Episode(title=title, segments=segs, wav_path=wav)


def _safe_make_episode(
    style: str, llm: str, model: str, max_budget_usd: float, speed: float
) -> Episode:
    """失敗時にリトライしてエピソードを生成する。"""
    last_error: Exception | None = None
    for attempt in range(1, EPISODE_MAX_RETRIES + 1):
        try:
            return make_episode(style, llm, model, max_budget_usd, speed)
        except Exception as e:
            last_error = e
            _log(f"  [エピソード生成エラー (試行{attempt}/{EPISODE_MAX_RETRIES}): {e}]")
            if attempt < EPISODE_MAX_RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(f"エピソード生成が{EPISODE_MAX_RETRIES}回失敗") from last_error


# ---------------------------------------------------------------------------
# main: 再生中に次を先読み
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="AIラジオ (Wikipedia x VOICEVOX x Claude/Ollama)"
    )
    ap.add_argument(
        "--style",
        choices=["solo", "duo"],
        default="duo",
        help="番組スタイル (既定: duo=二人掛け合い)",
    )
    ap.add_argument(
        "--llm",
        choices=["claude", "ollama"],
        default="claude",
        help="台本生成のLLM (既定: claude)",
    )
    ap.add_argument(
        "--model",
        default=None,
        help=f"モデル名 (既定: claude={DEFAULT_CLAUDE_MODEL} / ollama={DEFAULT_OLLAMA_MODEL})",
    )
    ap.add_argument(
        "--max-budget-usd",
        type=float,
        default=DEFAULT_MAX_BUDGET_USD,
        dest="max_budget_usd",
        help=f"Claude利用時の1エピソードあたり最大課金額(USD) (既定: {DEFAULT_MAX_BUDGET_USD})",
    )
    ap.add_argument(
        "--music-dir",
        default=None,
        help="トーク間に流す音楽ファイルのディレクトリ (サブディレクトリも再帰検索)",
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED_SCALE,
        help=f"話す速度 (既定: {DEFAULT_SPEED_SCALE} / 0.8=遅め 1.0=標準 1.2=速め)",
    )
    args = ap.parse_args()

    if args.model is None:
        args.model = (
            DEFAULT_CLAUDE_MODEL if args.llm == "claude" else DEFAULT_OLLAMA_MODEL
        )

    _log(
        f"放送開始  style={args.style} llm={args.llm} model={args.model} speed={args.speed}  (Ctrl-Cで停止)\n"
    )

    music_files: list[str] = []
    music_recent: deque[str] = deque(maxlen=MUSIC_RECENT_LIMIT)
    if args.music_dir:
        music_files = load_music_cache(args.music_dir)
        _log(f"  音楽ファイル {len(music_files)} 件を読み込みました\n")

    episode_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    music_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _log("最初のエピソードを生成中...")
    current = _safe_make_episode(
        args.style, args.llm, args.model, args.max_budget_usd, args.speed
    )

    nxt_ep: concurrent.futures.Future[Episode] | None = None
    nxt_music: concurrent.futures.Future[MusicInterlude | None] | None = None
    try:
        while True:
            # エピソード生成と音楽イントロ合成を並列で先読み
            nxt_ep = episode_pool.submit(
                _safe_make_episode,
                args.style,
                args.llm,
                args.model,
                args.max_budget_usd,
                args.speed,
            )
            if music_files:
                nxt_music = music_pool.submit(
                    _prepare_music_interlude, music_files, args.speed, music_recent
                )

            _log(f"\nオンエア: 「{current.title}」")
            play(current.wav_path)
            _safe_unlink(current.wav_path)

            # イントロは合成済みなので即再生
            if nxt_music is not None:
                try:
                    interlude = nxt_music.result()
                    nxt_music = None
                    if interlude:
                        play_prepared_interlude(interlude, args.speed)
                except Exception as e:
                    _log(f"  [音楽インタールードエラー: {e}]")
                    nxt_music = None

            try:
                current = nxt_ep.result()
                nxt_ep = None
            except Exception as e:
                _log(f"  [先読みエピソード失敗: {e}、同期生成を試みます]")
                nxt_ep = None
                current = _safe_make_episode(
                    args.style, args.llm, args.model, args.max_budget_usd, args.speed
                )
    except KeyboardInterrupt:
        _log("\n\n📴 放送終了。おやすみなさい。")
        _safe_unlink(current.wav_path)
        episode_pool.shutdown(wait=False, cancel_futures=True)
        music_pool.shutdown(wait=False, cancel_futures=True)
        # 先読み中だったエピソードの一時ファイルをクリーンアップ
        if nxt_ep is not None and nxt_ep.done() and not nxt_ep.cancelled():
            try:
                _safe_unlink(nxt_ep.result().wav_path)
            except Exception:
                pass
        # 事前合成済みイントロwavのクリーンアップ
        if nxt_music is not None and nxt_music.done() and not nxt_music.cancelled():
            try:
                result = nxt_music.result()
                if result:
                    _safe_unlink(result.intro_wav)
            except Exception:
                pass
        # バックグラウンドスレッドの終了を待たず即座に終了する。
        # wait=False でも Python の atexit がスレッドを join しようとするため、
        # 2回目の Ctrl+C でエラーが出るのを防ぐ。
        os._exit(0)


if __name__ == "__main__":
    main()
