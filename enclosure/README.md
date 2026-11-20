# Enclosure for the board and a Dayton ND65

A printed case that holds the ReSpeaker Core v2.0 and a Dayton Audio ND65-4 driver as one
table-top unit: 110 mm across its corners, 97 mm across its flats, 151 mm tall.

Status: designed and checked in CAD only. Nothing has been printed or measured yet.

The [TPU board mount variant](PCB-ISOLATION.md) adds a screw-free suspension for the microphone board, with a revised deck and a small fit gauge.

## The shape, and why

A hexagonal column, the same outline as the board, in three parts:

| Part | What it is | Height |
|---|---|---|
| `deck` | The board bay. Its floor is the lid of the speaker chamber. | 22.6 mm |
| `body` | The sealed speaker chamber. The driver sits on the baffle at its lower end and fires down. | 106 mm |
| `base` | A plate with a cone under the driver, carried on six pillars. | 22 mm |

The choices follow from what the microphones and the echo canceller need:

- **The board is on top, microphones up, with nothing above it.** The rim of the deck is
  level with the top face of the board, so no wall stands above a microphone to reflect sound
  into it. The LEDs and the user button stay in reach.
- **The driver fires down, away from the microphones,** into an 18 mm gap. A cone turns the
  sound outwards all round. The driver is on the centre line, so its sound reaches all six
  microphones over the same path. That is the easy case for the echo canceller.
- **The chamber is closed, and the board is outside it.** The chamber floor of the deck
  separates them, so the pressure behind the driver does not reach the microphones.
- **The board stands on four posts,** 17 mm above the deck floor, clear of the tallest socket
  (the stacked USB socket, 15.3 mm). Soft washers under the board at the posts keep vibration
  of the case out of the microphones.

## The speaker chamber

The chamber holds 0.59 litres behind the driver. With Dayton's figures for the ND65-4
(Fs 89.5 Hz, Qts 0.61, Vas 0.53 l) a closed box of that size gives:

| | 0.59 l (this case) | 1.4 l (Dayton's suggestion) |
|---|---|---|
| Resonance in the box | 123 Hz | 105 Hz |
| Qtc | 0.84 | 0.72 |
| 3 dB down at | about 107 Hz | about 104 Hz |

So the small chamber costs almost nothing at the low end; it has a rise of under 1 dB near
130 Hz. `CHAMBER` in the script sets the inside height: every 15 mm adds about 0.1 litre.

## Openings in the deck

All openings are cut down from the rim, so the board drops in from above with its sockets.
North is the edge with the USB and Ethernet sockets.

| Flat | Opening for |
|---|---|
| North | two USB sockets, Ethernet (they stand 4.7 mm past the board edge) |
| South | power, OTG, headset |
| North-east | micro SD card |
| North-west | HDMI |
| South-west, south-east | four slots each, for air |

## Parts to buy

| Count | Part | Where |
|---|---|---|
| 16 | M3 heat-set insert, 5 mm outside diameter, 4 mm long | 12 in the body's corner columns, 4 in the baffle |
| 6 | M3 x 14 screw | base to body, from below |
| 6 | M3 x 8 screw | deck to body |
| 4 | M3 x 6 screw with washer | driver to baffle, from inside the chamber |
| 4 | M3 x 8 screw, 4 soft washers | board to posts; the screw cuts its own thread |
| 3 | stick-on foot, 10 mm | under the base |
| | 1 mm foam tape or a bead of sealant | between deck and body, and under the driver flange |

## Putting it together

1. Set the inserts: six in each end of the body's columns, four in the baffle from inside.
2. Seat the driver on the baffle from inside the chamber, cone down, with tape under the flange.
3. Solder a lead to the driver and pass it through the hole in the deck floor.
4. Screw the deck to the body with tape between them. Seal the lead hole with glue.
5. Screw the base on from below.
6. Plug the lead into the board's speaker socket and screw the board to its posts.

Stuffing the chamber loosely with polyester wadding is worth doing.

## Printing

Each part is designed to print without supports, standing as it stands in the finished case. The walls are
3.2 mm. The body is the largest part at 110 x 97 x 106 mm and takes about 180 cm³ of plastic.

## Building the models

    pip install build123d
    python3 enclosure.py out/

This writes `base`, `body` and `deck` as STEP and STL, and `assembly.step` with simple
stand-ins for the board, the driver and the screws. It also reports any two parts that overlap and the
volume of the chamber. Every dimension is a named constant at the top of the script.

## Where the dimensions come from

- Board: Seeed's dimension drawing (hexagon 88 mm across flats, corners rounded 5 mm, four
  3 mm holes, socket positions and heights). The hole positions were checked against the
  images inside Seeed's SketchUp model.
- Driver: Dayton's STEP model (flange 64 mm square, four holes on a 46.85 mm square, 44 mm
  deep behind the flange) and Dayton's data sheet. The real model was placed in the body to
  check that it fits; the published assembly uses a stand-in, not Dayton's file.

## Not yet known

- The board's sockets were placed from a drawing. Check the openings against the real board
  before a long print; the deck alone is a short test print.
- The wifi aerial is at the west corner of the board. One deck screw and its brass insert sit
  17 mm below it. If the wifi range drops, leave that one screw out.
- There is no place set aside for a separate amplifier. The deck floor has about 40 x 40 mm
  clear under the processor module, 13 mm high.
