# Accept positive recordings

This folder is for 20 manually recorded positive WAV files:

- `accept-01.wav` to `accept-20.wav`
- `positive_lines.tsv` maps each WAV filename to the sentence to read.
- Audio format produced by the recorder: 44.1 kHz, mono, 16-bit PCM WAV.

Start recording from the project root:

```bash
display/scripts/record_accept_positive_lines.sh
```

Record each line for exactly 5 seconds:

```bash
display/scripts/record_accept_positive_lines.sh --seconds 5
```

Continue from sentence 8:

```bash
display/scripts/record_accept_positive_lines.sh --from 8
```

Play confirmations through an AGX-connected speaker device. The default is the HDMI monitor sink; override it when needed:

```bash
display/scripts/record_accept_positive_lines.sh --playback-device plughw:0,3
```

After each recording, the script plays the WAV immediately. Press Enter to accept, `r` to record that line again, `p` to play it again, or `q` to quit.
