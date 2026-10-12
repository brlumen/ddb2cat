# ddb2cat

A command-line tool that turns **your own copy** of the Toyota Techstream diagnostic database into a plain-text **Data List catalog** for one vehicle. The catalog lists:
- the vehicle's ECUs and their CAN addresses;
- every live-data parameter, with the request that reads it, its bit position, scaling, unit and value labels;
- DTC descriptions.

The catalog is meant for your own tools that poll ECUs through an ELM327/STN adapter (OBDLink and similar). With it, such a tool can show the same Data List as Techstream without Techstream.

This repository contains **no Toyota data**. The tool reads the database files that you already have.

## Status

| What | State |
|---|---|
| KWP on CAN ECUs (Techstream "P4", requests `21 LID`) | verified on a real vehicle (2018 Toyota Passo, 16 ECUs) |
| CAN addresses from the database | match every ECU address found on that vehicle |
| UDS ECUs (Techstream "P5", newer vehicles, requests `22 DID`, DTC `19 02`) | parsed, **not verified on a real vehicle yet** |
| ECUs behind the central gateway (extended addressing, address `750/xx`) | parsed, **not verified on a real vehicle yet** |
| K-line and Ethernet (DoIP) ECUs | listed, but have no CAN address (`?` in listings) |

Reports from UDS vehicles are welcome. Open an issue and say which parameters read correctly.

## Requirements

- Python 3.9 or later, no third-party packages.
- A copy of the Techstream install folder. The tool reads `<region>/DB/*.ddb` (regions `JP`, `EU`, `NA`). The format was worked out on the Techstream 17.00 database.

## Usage

Find the model, then the vehicle variant, then build its catalog:

```sh
python3 ddb2cat.py models   "/path/to/Techstream" --model corolla
python3 ddb2cat.py vehicles "/path/to/Techstream" --model "corolla touring" --region JP
python3 ddb2cat.py build    "/path/to/Techstream" --vehicle 57CB -o corolla.txt
```

- **`models`** lists every model that has ECUs in the database, with the number of variants per region. It also shows how many of those variants include UDS ECUs.
- **`vehicles`** lists the variants: region, vehicle id, model and ECU files. Variants of one model differ in their ECU sets. The marks are:
  - `*`: UDS ECU;
  - `^`: behind the gateway;
  - `?`: no CAN address in the database.
- **`build`** writes the catalog of one vehicle, to stdout when `-o` is not given. Pass `--region` when the vehicle id exists in more than one region.

Reading the strings and decompressing sections takes a few seconds per region.

## Catalog format (version 2)

The catalog is UTF-8 text with one record per line and fields separated by `|`. A `|` inside a value is replaced by `/`.

```
#catalog|2|vehicle=6A9D|region=JP|model=PASSO
B|id|name|address|probes|protocol
L|LID|length
P|section|name|database name|request|bit1|bit2|signed|mul|div|add|dec|unit|values
D|code|description
```

**`B` — ECU.** The following `L`, `P` and `D` lines belong to this ECU.
- `address`: the CAN id of requests, in hex. Replies come from `address + 8`.
  - For an ECU behind the central gateway the address is `750/xx`: requests go to `750` and the first data byte of every CAN frame is the extended address `xx`. With ELM327/STN this is `AT CEA xx`; switch it off afterwards with `AT CEA`.
  - An empty address means it is unknown. To find it, send the `probes` to candidate addresses: these are the rarest requests of this ECU, and the ECU answers them positively.
- `protocol`: `KWP` (read with `21 LID`, DTC with `13 80`) or `UDS` (read with `22 DID`, DTC with `19 02`).

**`L` — full reply length** of `21 LID` without the `61 LID` bytes. KWP only, see "Packed KWP replies" below.

**`P` — parameter.**
- `request`: `21xx` (KWP, LID) or `22xxxx` (UDS, DID). The positive reply is `61 LID data…` or `62 DID data…`.
- `bit1..bit2`: the bits of the field in `data`, counted from the most significant bit of the first data byte (bit 0 = `0x80` of byte 0, bit 8 = `0x80` of byte 1). The field is read most significant bit first.
- `signed` = 1: two's complement over the field width.
- value = `(raw × mul / div + add) / 10^dec`, printed with `dec` decimals.
- `values`: labels for enumerations, `raw=label;raw=label`. When present, show the label instead of the number.
- `name`, `unit`, `values`: as in the database (English).
- `section` and `database name` are empty. They are there for your own post-processing: a tool that translates names or groups parameters into sections writes the section title into `section` and moves the original name into `database name`.

Example: `P|…|Engine Speed||221F0C|0|15|0|1|4|0|0|rpm|`. The reply `62 1F 0C 0B B8` gives raw `0x0BB8` = 3000 → 3000 / 4 = **750 rpm**.

**`D` — DTC description.**
- KWP codes have 5 characters (`P0300`).
- UDS codes have 7: the code plus the failure type byte (`P055512`).
- A `19 02` reply is `59 02 mask` followed by 4-byte records `[code hi][code lo][failure type][status]`. The first two bytes use SAE J2012 encoding: top 2 bits P/C/B/U, then the digits.

**Packed KWP replies.** Many KWP ECUs omit unsupported bytes, so the reply is shorter than `L`. The positions in the catalog are for the full reply. To restore it:
1. Send `A8 01`. The reply `E8 01` is followed by records `[LID][N][N mask bytes]`, possibly in several ISO-TP messages. With STN, wait for all of them with a fixed timeout (`AT AT0`).
2. Walk the mask of the LID: a non-zero byte takes the next byte of the reply, a zero byte is absent.
3. A parameter with no bits set in the mask is not supported by this ECU. Techstream hides it.

## Database format

These notes come from analysing the database files and are here for maintenance.

- **`*.ddb`.** Header `DiagTool DataCtrl`; section offsets (u32) from `0x24`.
  - A section is `[u16 type][u32 count][u32 size]` + records of equal length.
  - Type `| 0x100` means compressed: `[u16 type][u32 count][u32 packed][u32 size][u8]` + Okumura LZSS (window 4096, start `0xFEE`).
  - The EU database and the string tables are compressed.
- **`M_English.ddb`.** Section 0: `[u32 offset][u16 length]` per string + UTF-16LE text. String id = index + 1.
- **`Toyota.ddb`.**
  - `0x2B` vehicles: `[u32 model name][u32 vehicle id]`.
  - `0x17` vehicle → ECU: `[u32 ECU][u32][u32][u32][u32 vehicle]`.
  - `0x10` ECU → file: 60 bytes UTF-16 file name, `+0x3C` u32 title, `+0x44` u32 ECU, `+0x48` u32 category.
  - `0x0D` ECU → bus (u16): `+0` ECU, `+8` request CAN id, `+0xA` extended address, `+0x18` bus. Bus `0x12` is 11-bit CAN, `0x22` is the same with extended addressing.
- **KWP ECU file.**
  - `0x06` LID → full reply length.
  - `0x0A` parameters, at `+0x30`: name, index, formula, bits, enumeration, kind, Data List flag, LID.
  - `0x05` DTC: 6-char UTF-16 code + description.
- **UDS ECU file.**
  - `0x3D` DIDs.
  - `0x3E` parameters: `+0x18` name, `+0x20` flags (bit 0 = Data List), `+0x2A` formula, `+0x2C`/`+0x2E` bits, `+0x32` enumeration, `+0x36` DID, `+0x3A` kind.
  - `0x41` DTC: UTF-16 code, `+0x30` description, `+0x34` failure type.
- **Both kinds.**
  - `0x0D` formulas `[mul][div][add][id][unit]…[signed][dec]`.
  - `0x0E` enumeration values.
  - `0x0F` units.
- **Parameter kind.** Low byte: 1 enumeration, 2 number. Flag `0x200` means hidden in the Data List.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests build a small synthetic database (KWP, UDS, gateway and K-line ECUs, compressed sections) and check the whole catalog. No Techstream files are needed.

## Legal

- This project is not affiliated with or endorsed by Toyota Motor Corporation. Toyota and Techstream are trademarks of their respective owners.
- The repository contains only code written for interoperability: reading a database you already have. It contains no part of that database.
- Catalogs you generate contain data from your copy of the database. Keep them for your own use and do not publish them.
- The tool itself never talks to a vehicle.

## License

[MIT](LICENSE)
