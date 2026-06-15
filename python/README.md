# UCCBViewer LIN (Python)

Python/Tkinter port of the Java **UCCBViewer**, reduced to **LIN-only**
operation — a tool for monitoring, sending and scheduling LIN frames with the
UsbCANConverterBasic (uCCB) adapter. It speaks the same **SLCAN** serial
protocol as the original.

## Features

- Connect/disconnect over a serial port with selectable LIN baud rate
  (19200 / 9600) and open mode (MASTER / MONITOR / SLAVE).
- **Trace** tab – full timestamped log with info/error/in/out icons.
- **Monitor** tab – one row per id showing period and message count, with a
  per-nibble heat map: data nibbles that change often are drawn red, constant
  ones stay the default green (the colour decays back to green once a nibble
  settles).
- Dark theme throughout (near-black background, cyan section headers, green
  monospace tables).
- **LIN Schedule Table** tab – 15 configurable rows, SCAN, Clear/Send table.
- Message builder (id + 8 data bytes + DLC + "No data") kept in sync with the
  raw SLCAN message string.
- Repeat sending with configurable interval.
- HEX/DEC display toggle.
- File logging in either SLCAN or CSV format (timestamped `*.log` file in the
  working directory).
- Right-click a trace row to Resend or Copy.
- Classic / enhanced LIN checksum selection.

## Install

```bash
cd python
pip install -r requirements.txt
```

(Tkinter ships with CPython. On some Linux distros install it separately, e.g.
`sudo apt install python3-tk`.)

## Run

```bash
cd python
python -m uccbviewer
```

## Notes on the SLCAN / protocol layer

`usbtin.py` and `can_message.py` are a direct port of
[uCCBlib](https://github.com/UsbCANConverter-UCCbasic/uCCBlib) (a fork of Thomas
Fischl's USBtinLIB). The wire protocol matches the Java sources byte-for-byte:

- device queries `v`/`V`/`N` and the `W2D00` overflow-flag reset on connect;
- the channel bitrate is selected with `S0`–`S9` (LIN baud is mapped onto the
  internal rate the firmware expects), arbitrary rates via the `s` command using
  the same 24 MHz FOSC register calculation (`USBtin._calculate_btr`);
- open modes `O`/`l`/`L` (MASTER / MONITOR / SLAVE), close `C`;
- `t`/`T`/`r`/`R` frame encoding/decoding (remote frames keep their DLC).

LIN mode reuses the `r…` command frames exactly as the Java application does
(`r30`/`r31` checksum select, `r2FF0` reset table, schedule rows, `r1FF0` start).
