# Reject roast recordings

This folder is for the 30 manually recorded roast WAV files:

- `reject-01.wav` to `reject-30.wav`
- `roast_lines.tsv` maps each WAV filename to the sentence to read.
- Audio format produced by the recorder: 44.1 kHz, mono, 16-bit PCM WAV.

Start recording from the project root:

```bash
display/scripts/record_reject_roasts.sh
```

Record each line for exactly 5 seconds:

```bash
display/scripts/record_reject_roasts.sh --seconds 5
```

Continue from sentence 12:

```bash
display/scripts/record_reject_roasts.sh --from 12
```

After each recording, the script plays the WAV immediately. Press Enter to accept, `r` to record that line again, `p` to play it again, or `q` to quit.
