# wikiradio

Wikipediaのランダム記事をネタに、AIが台本を書いてVOICEVOXが読み上げるラジオを延々と流すスクリプト。

## 事前準備

### 1. uv のインストール

[Installation | uv](https://docs.astral.sh/uv/getting-started/installation/)

### 2-1. Claude CLI のインストール・ログイン(Claude Code利用時のみ)

[高度なセットアップ - Claude Code Docs](https://code.claude.com/docs/ja/setup)

### 2-2. Ollama のセットアップ（Ollama利用時のみ）

[Download Ollama on macOS](https://ollama.com/download)

```
# モデルを取得して起動
ollama pull gemma4:latest
ollama serve
```

### 3. VOICEVOX Engine の起動

Docker が必要です。以下のコマンドを別ターミナルで実行したままにしておきます。

```bash
# CPU版（M1/M2 Mac を含む）
docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest
```

起動確認：

```bash
curl -s http://localhost:50021/version
```

バージョン番号が返れば OK。

## 実行

```bash
uv run wiki_radio.py
```

## オプション

| オプション                      | 既定値          | 説明                                                         |
| ------------------------------- | --------------- | ------------------------------------------------------------ |
| `--style duo` / `--style solo`  | `duo`           | 二人掛け合い / DJ一人語り                                    |
| `--llm claude` / `--llm ollama` | `claude`        | 台本生成のLLM                                                |
| `--model <モデルID>`            | llmに応じて自動 | モデル名（Claude/Ollamaどちらも共通オプション）              |
| `--max-budget-usd <金額>`       | `0.05`          | Claude利用時の1エピソードあたり最大課金額（USD）             |
| `--music-dir <パス>`            | なし            | トーク間に流す音楽ディレクトリ（サブディレクトリも再帰検索） |

Claude利用時は各エピソード生成後に実際の費用が `💰 Claude費用: $0.0123` 形式で表示されます。

### 実行例

```bash
# Ollamaで実行 (ollama serve が起動済みであること)
uv run wiki_radio.py --llm ollama

# Ollamaのモデルを指定
uv run wiki_radio.py --llm ollama --model llama3.2:3b

# 高品質Claudeモデルで二人掛け合い
uv run wiki_radio.py --model claude-sonnet-4-6

# 課金上限を下げてコスト抑制
uv run wiki_radio.py --max-budget-usd 0.02

# トークの間に音楽を挟む（サブディレクトリも再帰検索）
uv run wiki_radio.py --music-dir ~/Music

# 環境変数でデフォルトを変える
CLAUDE_MODEL=claude-sonnet-4-6 CLAUDE_MAX_BUDGET_USD=0.10 uv run wiki_radio.py
OLLAMA_MODEL=llama3.2:3b uv run wiki_radio.py --llm ollama
```

### 音楽ディレクトリについて

`--music-dir` に指定したディレクトリ内の対応ファイル（mp3 / m4a / aac / flac / wav / aiff / ogg）をランダムに1曲選んで再生します。

ファイル名から曲情報を読み取ります：

| ファイル名               | アーティスト | 曲名  |
| ------------------------ | ------------ | ----- |
| `Artist - Title.mp3`     | Artist       | Title |
| `01. Artist - Title.mp3` | Artist       | Title |
| `Title.mp3`              | （なし）     | Title |

停止は `Ctrl-C`。

## カスタマイズ

### 話者（声）の変更

VOICEVOX の話者IDは環境ごとに異なります。一覧を確認して設定してください。

```bash
curl -s http://localhost:50021/speakers | python3 -m json.tool | grep -E '"name"|"id"'
```

```bash
SPEAKER_A=3 SPEAKER_B=2 uv run wiki_radio.py
```

### 台本スタイルの変更

`wiki_radio.py` 冒頭の `PROMPT_DUO` / `PROMPT_SOLO` 定数を書き換えるのが最もキャラクターの雰囲気に効きます。

## アーキテクチャ

```
Wikipedia Random API
  └─ 本文長フィルタ（1500文字未満はリトライ）
       └─ claude -p で台本生成 → [{speaker, text}, ...]
            └─ VOICEVOX HTTP API でセグメントごとに wav 生成 → 結合
                 └─ afplay (macOS) / ffplay で再生
                      └─ 再生中に裏で次のエピソードを先読み (ThreadPoolExecutor)
```
