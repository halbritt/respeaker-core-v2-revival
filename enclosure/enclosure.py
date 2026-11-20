#!/usr/bin/env python3
"""Enclosure for the ReSpeaker Core v2.0 and a Dayton ND65 driver.

Three printed parts, stacked:

  deck  board bay; its floor is the lid of the speaker chamber
  body  sealed speaker chamber, driver on the baffle at its lower end, firing down
  base  plate with a cone that turns the sound sideways, on six pillars

Run with build123d installed:  python3 enclosure.py [output directory]
All lengths are millimetres. x is east, y is north (the USB and Ethernet edge of the board),
z is up. The board sits microphones up, as it does bare on a table.
"""
import math
import sys
from pathlib import Path

from build123d import (
    Align, Axis, Box, Compound, Cone, Cylinder, Part, Plane, Polygon, Pos, RegularPolygon, Rot,
    export_step, export_stl, extrude, fillet, revolve,
)

# ---- the board (Seeed's dimension drawing, see README) --------------------------------
BOARD_FLATS = 88.0            # hexagon, across flats; corners on the east-west line
BOARD_THICK = 1.6
BOARD_HOLES = [(-23.8, 14.9), (23.8, 14.9), (-29.15, -31.2), (29.15, -31.2)]
BOARD_HOLE = 3.0              # drill in the board, for M3
UNDER_BOARD = 17.0            # tallest part under the board is the USB socket, 15.3

# ---- the driver (measured from Dayton's model) -------------------------------------------
DRIVER_HOLES = 46.85          # square pattern, centre to centre
DRIVER_CUTOUT = 55.0          # clears the surround, which stands 3.5 proud of the flange
DRIVER_FLANGE = 65.0
DRIVER_DEPTH = 44.0           # behind the flange

# ---- the enclosure -------------------------------------------------------------------------
GAP = 1.0                     # between board edge and wall
WALL = 3.2
INNER_FLATS = BOARD_FLATS + 2 * GAP
OUTER_FLATS = INNER_FLATS + 2 * WALL
EDGE_RADIUS = 6.0             # rounding of the six vertical edges, outside

BAFFLE = 6.0
CHAMBER = 100.0               # inside height of the speaker chamber
DECK_FLOOR = 4.0
BASE_PLATE = 4.0
THROAT = 18.0                 # gap under the driver where the sound leaves
CONE_HEIGHT = 12.0

BOSS = 10.0                   # column in each corner that takes the screws
BOSS_AT = INNER_FLATS / math.sqrt(3) - 5.2
INSERT = 4.0                  # hole for an M3 heat-set insert (5 mm outside, 4 mm long)
INSERT_DEPTH = 6.0
M3_CLEAR = 3.4
LEAD_HOLE = (19.0, -24.0)     # where the speaker lead comes up through the deck floor

BODY_H = BAFFLE + CHAMBER
DECK_H = DECK_FLOOR + UNDER_BOARD + BOARD_THICK   # rim level with the top of the board


def corner(flats):
    return flats / math.sqrt(3)


LOW = (Align.CENTER, Align.CENTER, Align.MIN)     # standing on its position
HIGH = (Align.CENTER, Align.CENTER, Align.MAX)    # hanging from its position


def prism(flats, height, radius=0.0):
    """Hexagonal prism standing on z = 0, corners on the x axis."""
    solid = extrude(RegularPolygon(corner(flats), 6), amount=height)
    if radius > 0:
        solid = fillet(solid.edges().filter_by(Axis.Z), radius)
    return solid


def outer(height):
    return prism(OUTER_FLATS, height, EDGE_RADIUS)


def inner(height):
    return prism(INNER_FLATS, height, max(EDGE_RADIUS - WALL, 0.5))


def ring(radius, count, shape, start=0.0):
    """`count` copies of `shape` on a circle around the z axis."""
    return [Rot(0, 0, start + k * 360 / count) * Pos(radius, 0, 0) * shape for k in range(count)]


def on_flat(angle, along, width, bottom, top):
    """A box through the wall of the flat whose outward normal is at `angle` degrees,
    `along` millimetres from the middle of that flat (positive is anticlockwise)."""
    depth = WALL + 6
    box = Box(depth, width, top - bottom, align=LOW)
    return Rot(0, 0, angle) * Pos(OUTER_FLATS / 2 - depth / 2 + 1, along, bottom) * box


def columns(radius, height):
    """Six corner columns, kept inside the outer skin."""
    return Part() + ring(BOSS_AT, 6, Cylinder(radius, height, align=LOW)) & outer(height)


def body():
    h = DRIVER_HOLES / 2
    solid = outer(BODY_H) - Pos(0, 0, BAFFLE) * inner(CHAMBER + 1)
    solid += columns(BOSS / 2, BODY_H)
    # driver opening, flared on the outside
    solid -= Cylinder(DRIVER_CUTOUT / 2, BAFFLE, align=LOW)
    solid -= Cone(DRIVER_CUTOUT / 2 + 2, DRIVER_CUTOUT / 2, 2, align=LOW)
    # inserts for the driver, set from inside the chamber
    solid -= [Pos(x, y, BAFFLE) * Cylinder(INSERT / 2, BAFFLE - 1, align=HIGH)
              for x in (-h, h) for y in (-h, h)]
    # inserts in the columns: base screws from below, deck screws from above
    solid -= ring(BOSS_AT, 6, Cylinder(INSERT / 2, INSERT_DEPTH, align=LOW))
    solid -= ring(BOSS_AT, 6, Pos(0, 0, BODY_H) * Cylinder(INSERT / 2, INSERT_DEPTH, align=HIGH))
    return solid


def deck():
    floor_top = DECK_FLOOR
    board_under = DECK_FLOOR + UNDER_BOARD
    solid = outer(DECK_H) - Pos(0, 0, floor_top) * inner(DECK_H)
    # posts under the board's four holes; an M3 screw cuts its own thread in each
    solid += [Pos(x, y, floor_top) * Cylinder(3.2, UNDER_BOARD, align=LOW) for x, y in BOARD_HOLES]
    solid -= [Pos(x, y, board_under) * Cylinder(1.3, 10, align=HIGH) for x, y in BOARD_HOLES]
    # screws into the body's columns
    solid -= ring(BOSS_AT, 6, Cylinder(M3_CLEAR / 2, DECK_FLOOR, align=LOW))
    # speaker lead, sealed with glue after wiring
    solid -= Pos(*LEAD_HOLE, 0) * Cylinder(2.5, DECK_FLOOR, align=LOW)
    # openings, all open at the top so the board drops in with its sockets
    solid -= on_flat(90, 0, 40, floor_top + 1, DECK_H + 1)         # USB, Ethernet
    solid -= on_flat(270, 0, 40, board_under - 9, DECK_H + 1)      # power, OTG, headset
    solid -= on_flat(30, -10, 17, board_under - 6, DECK_H + 1)     # SD card
    solid -= on_flat(150, -10, 19, board_under - 9, DECK_H + 1)    # HDMI
    # air for the processor on the two closed flats
    solid -= [on_flat(angle, along, 3, floor_top + 3, board_under - 3)
              for angle in (210, 330) for along in (-9, -3, 3, 9)]
    return solid


def base():
    total = BASE_PLATE + THROAT
    solid = outer(BASE_PLATE) + columns(BOSS / 2 + 0.5, total)
    # the cone under the driver
    profile = Polygon((0, 0), (DRIVER_CUTOUT / 2 + 1, 0), (4, CONE_HEIGHT), (0, CONE_HEIGHT),
                      align=None)
    solid += Pos(0, 0, BASE_PLATE) * revolve(Plane.XZ * profile, Axis.Z)
    # M3 x 16 from below into the body, heads sunk into the pillars
    solid -= ring(BOSS_AT, 6, Cylinder(M3_CLEAR / 2, total, align=LOW))
    solid -= ring(BOSS_AT, 6, Cylinder(3.2, total - 9, align=LOW))
    # three stick-on feet
    solid -= ring(30, 3, Cylinder(5.5, 1, align=LOW), start=30)
    return solid


def board_stand_in():
    """The board as a plate with its tall sockets, to check the fit. Not for printing."""
    solid = prism(BOARD_FLATS, BOARD_THICK, 5)
    solid -= [Pos(x, y, 0) * Cylinder(BOARD_HOLE / 2, BOARD_THICK, align=LOW) for x, y in BOARD_HOLES]

    def under(x0, x1, y0, y1, h):
        return Pos((x0 + x1) / 2, (y0 + y1) / 2, 0) * Box(x1 - x0, y1 - y0, h, align=HIGH)

    solid += [
        under(2.6, 17.3, 31.0, 48.7, 15.3),      # two USB sockets, stacked; they overhang the edge
        under(-17.1, -1.2, 27.0, 48.7, 13.5),    # Ethernet
        under(8.1, 16.1, -44.5, -39.0, 2.7),     # power
        under(-3.9, 4.1, -44.5, -39.0, 2.7),     # OTG
        under(-15.6, -9.6, -45.7, -31.0, 5.2),   # headset
        under(-20.0, 20.0, -25.0, 23.0, 3.0),    # processor module
    ]
    return solid


def driver_stand_in():
    """The driver as turned shapes: flange, basket, magnet. Flange underside on z = 0."""
    flange = Box(64, 64, 1.2, align=LOW)
    solid = fillet(flange.edges().filter_by(Axis.Z), 9)
    solid += Cylinder(25.5, 14, align=LOW)
    solid += Pos(0, 0, 14) * Cone(25.5, 17.75, 8, align=LOW)
    solid += Cylinder(17.75, DRIVER_DEPTH, align=LOW)
    solid += Cylinder(26.9, 3.5, align=HIGH)                # surround, in front of the flange
    solid += Pos(0, 28, 5) * Box(8, 6, 9, align=LOW)        # terminals
    return solid


def parts():
    """Every part in its place in the assembled stack, base on z = 0."""
    z_body = BASE_PLATE + THROAT
    z_deck = z_body + BODY_H
    return {
        "base": base(),
        "body": Pos(0, 0, z_body) * body(),
        "deck": Pos(0, 0, z_deck) * deck(),
        "board": Pos(0, 0, z_deck + DECK_FLOOR + UNDER_BOARD) * board_stand_in(),
        "driver": Pos(0, 0, z_body + BAFFLE) * driver_stand_in(),
    }


def chamber_litres(placed):
    """Air behind the driver: the closed space inside the body, less the driver."""
    z0 = BASE_PLATE + THROAT + BAFFLE
    space = Pos(0, 0, z0) * inner(CHAMBER) - placed["body"] - placed["driver"]
    return space.volume / 1e6


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    placed = parts()
    printed = {"base": base(), "body": body(), "deck": deck()}
    for name, part in printed.items():
        export_step(part, str(out / f"{name}.step"))
        export_stl(part, str(out / f"{name}.stl"), tolerance=0.02, angular_tolerance=0.2)
    whole = Compound(children=list(placed.values()))
    for (name, part) in placed.items():
        part.label = name
    export_step(whole, str(out / "assembly.step"))
    names = list(placed)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            clash = (placed[a] & placed[b])
            v = clash.volume if clash else 0.0
            if v > 0.5:
                print(f"CLASH {a} / {b}: {v:.1f} mm3")
    box = whole.bounding_box()
    print(f"overall {box.size.X:.1f} x {box.size.Y:.1f} x {box.size.Z:.1f} mm")
    print(f"chamber {chamber_litres(placed):.3f} litres")
    for name, part in printed.items():
        print(f"{name}: {part.volume / 1000:.0f} cm3 of plastic, one solid: {len(part.solids()) == 1}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out")
