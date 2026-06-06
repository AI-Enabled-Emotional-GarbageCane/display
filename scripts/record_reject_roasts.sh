#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINES_FILE="$ROOT_DIR/assets/audio/reject/roast_lines.tsv"
OUT_DIR="$ROOT_DIR/assets/audio/reject"
RATE=44100
CHANNELS=1

usage() {
  printf 'Usage: %s [--from N] [--seconds N] [--list] [--lines PATH] [--out-dir PATH]\n' "$(basename "$0")"
  printf '\n'
  printf 'Options:\n'
  printf '  --from N        Start from sentence N, 1-30. Default: 1\n'
  printf '  --seconds N     Record each line for exactly N seconds. Default: manual stop\n'
  printf '  --list          Show the recording queue and exit\n'
  printf '  --lines PATH    Use a different TSV file with filename<TAB>text\n'
  printf '  --out-dir PATH  Write WAV files to a different directory\n'
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

play_wav() {
  local wav_path="$1"

  if command -v aplay >/dev/null 2>&1; then
    aplay -q "$wav_path"
  elif command -v ffplay >/dev/null 2>&1; then
    ffplay -nodisp -autoexit -loglevel error "$wav_path"
  elif command -v play >/dev/null 2>&1; then
    play -q "$wav_path"
  else
    printf '找不到播放工具，請安裝 aplay、ffplay 或 sox/play。\n' >&2
    return 1
  fi
}

record_wav() {
  local target="$1"
  local seconds="$2"
  local tmp_path
  tmp_path="$(mktemp --suffix=.wav)"

  if [[ "$seconds" -gt 0 ]]; then
    printf '錄音中...%d 秒後自動停止。\n' "$seconds"
    arecord -q -f S16_LE -c "$CHANNELS" -r "$RATE" -d "$seconds" "$tmp_path"
  else
    printf '錄音中...念完後按 Enter 停止。'
    arecord -q -f S16_LE -c "$CHANNELS" -r "$RATE" "$tmp_path" &
    local rec_pid=$!

    trap 'kill "$rec_pid" >/dev/null 2>&1 || true; rm -f "$tmp_path"' INT TERM
    read -r _
    kill -INT "$rec_pid" >/dev/null 2>&1 || true
    wait "$rec_pid" >/dev/null 2>&1 || true
    trap - INT TERM
  fi

  ffmpeg -y -hide_banner -loglevel error -i "$tmp_path" -ar "$RATE" -ac "$CHANNELS" -sample_fmt s16 "$target"
  rm -f "$tmp_path"
}

START_AT=1
RECORD_SECONDS=0
LIST_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      START_AT="${2:-}"
      shift 2
      ;;
    --seconds)
      RECORD_SECONDS="${2:-}"
      shift 2
      ;;
    --list)
      LIST_ONLY=1
      shift
      ;;
    --lines)
      LINES_FILE="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! [[ "$START_AT" =~ ^[0-9]+$ ]] || [[ "$START_AT" -lt 1 ]]; then
  printf '%s\n' '--from must be a positive number.' >&2
  exit 1
fi

if ! [[ "$RECORD_SECONDS" =~ ^[0-9]+$ ]]; then
  printf '%s\n' '--seconds must be 0 or a positive whole number.' >&2
  exit 1
fi

if [[ ! -f "$LINES_FILE" ]]; then
  printf '找不到台詞清單：%s\n' "$LINES_FILE" >&2
  exit 1
fi

need_cmd arecord
need_cmd ffmpeg
mkdir -p "$OUT_DIR"

mapfile -t LINES < <(tail -n +2 "$LINES_FILE")
TOTAL="${#LINES[@]}"

if [[ "$LIST_ONLY" -eq 1 ]]; then
  for i in "${!LINES[@]}"; do
    IFS=$'\t' read -r filename text <<< "${LINES[$i]}"
    printf '%02d. %s\t%s\n' "$((i + 1))" "$filename" "$text"
  done
  exit 0
fi

printf 'Roast WAV recorder\n'
printf '輸出資料夾：%s\n' "$OUT_DIR"
printf '格式：%s Hz, mono, 16-bit PCM WAV\n' "$RATE"
if [[ "$RECORD_SECONDS" -gt 0 ]]; then
  printf '錄音長度：每句固定 %d 秒\n' "$RECORD_SECONDS"
else
  printf '錄音長度：手動按 Enter 停止\n'
fi
printf '操作：每句錄完會自動播放；Enter 接受，r 重錄，p 再播放，q 離開。\n\n'

for i in "${!LINES[@]}"; do
  index="$((i + 1))"
  if [[ "$index" -lt "$START_AT" ]]; then
    continue
  fi

  IFS=$'\t' read -r filename text <<< "${LINES[$i]}"
  target="$OUT_DIR/$filename"

  while true; do
    printf '\n[%02d/%02d] %s\n' "$index" "$TOTAL" "$filename"
    printf '台詞：%s\n' "$text"
    if [[ -f "$target" ]]; then
      printf '注意：%s 已存在，這次接受錄音會覆蓋它。\n' "$target"
    fi
    printf '準備好後按 Enter 開始錄音，或輸入 q 離開：'
    read -r ready
    if [[ "$ready" == "q" || "$ready" == "Q" ]]; then
      printf '已離開。下次可用 --from %d 繼續。\n' "$index"
      exit 0
    fi

    record_wav "$target" "$RECORD_SECONDS"
    printf '已儲存：%s\n' "$target"
    printf '播放確認中...\n'
    play_wav "$target" || true

    while true; do
      printf '確認結果：Enter 接受 / r 重錄 / p 再播放 / q 離開：'
      read -r choice
      case "$choice" in
        '')
          break 2
          ;;
        r|R)
          break
          ;;
        p|P)
          play_wav "$target" || true
          ;;
        q|Q)
          printf '已離開。下次可用 --from %d 繼續。\n' "$index"
          exit 0
          ;;
        *)
          printf '請輸入 Enter、r、p 或 q。\n'
          ;;
      esac
    done
  done
done

printf '\n完成：%d 個 WAV 都已錄製。\n' "$TOTAL"
