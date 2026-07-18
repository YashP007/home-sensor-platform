# Enclosure Design

The enclosure is a two-piece FDM-printed housing designed for wall mounting via a dovetail slide. This document explains the design constraints, print orientation, and dimensional choices.

## Overall approach

The enclosure consists of two pieces:

1. **Back plate** — mounts to the wall via a 3M Command Strip or double-sided VHB tape. Contains the female dovetail slot on its front face and a small stop feature to prevent the box from sliding off the bottom.
2. **Main box** — houses the PCB. Contains the male dovetail rail on its back face, cutouts for the two barrel jacks and the XIAO's USB-C port, and internal M2 standoffs for mounting the PCB.

The two-piece approach separates the concerns of wall attachment (permanent, structural) from device servicing (frequent, temporary). To reprogram the XIAO or swap a sensor, the main box slides off the back plate one-handed, without disturbing the wall mount or the barrel jack cables.

## External dimensions

The main box exterior measures 50 × 80 × ~22 mm (W × H × D). The 50 × 80 footprint was chosen to accommodate the PCB (~45 × 50 mm) plus internal clearance for cable routing to the barrel jacks and the Qwiic connectors. The 22 mm depth accommodates the tallest component on the assembled PCB (the 100 µF electrolytic capacitor, ~11 mm) plus the barrel jack overhang and the dovetail rail height on the exterior.

The back plate matches the 50 × 80 footprint with 3 mm thickness plus the 4 mm-tall dovetail slot depth on its front face.

## Dovetail geometry

The dovetail is a standard 60° included angle (30° per wall from vertical), 4 mm tall, running along the 80 mm dimension of the enclosure (i.e., vertical when wall-mounted). Full dimensions:

| Parameter | Value |
|---|---|
| Included angle | 60° (30° per wall) |
| Depth | 4 mm |
| Wide face width (top of male, bottom of female) | 20 mm |
| Narrow face width (bottom of male, top of female) | 15.4 mm |
| Length | 75 mm (leaves 2.5 mm at each end of the 80 mm dimension) |
| Sliding clearance (angled faces) | 0.3 mm per side |
| Sliding clearance (bottom face) | 0.4 mm |
| Leading-edge chamfer on male rail | 1 mm × 45° |

The female slot is on the back plate; the male rail is on the main box. This orientation is chosen because the male rail's spreading force (from the box's weight pulling downward and outward) is resisted by the bulk material of the back plate, which is stronger under that load pattern than the thin walls of the main box would be.

A 2 mm × 3 mm bump at the bottom of the female slot acts as a hard stop, preventing the box from sliding off if bumped upward.

## Print orientation

Both parts print with their dovetail sliding axis parallel to the build plate, and the wide face of each dovetail feature facing downward toward the build plate.

**Back plate:** printed flat, with the front face (containing the female slot) facing up. The slot itself prints as a groove — no bridging, no supports required. The overhang inside the slot is at 30° from vertical, well within FDM bridging capability.

**Main box:** printed on its back face, with the male dovetail rail projecting upward into the print. The rail's 30° overhangs print cleanly without supports. The box's front face (which would have the sensor viewing window if a light sensor is added later) becomes the top of the print, giving it the smoothest visible surface.

This orientation is critical. Printing either part with the sliding axis vertical would put layer-line adhesion in shear under every insertion/removal cycle, and the dovetail would eventually split along a layer line. In the recommended orientation, the sliding force is oriented parallel to the layer lines, which handles shear stress far better than layer-to-layer adhesion.

## Material and print settings

**Recommended: PETG.** PETG-on-PETG has lower sliding friction than PLA-on-PLA and is more forgiving of dimensional inaccuracy. It also tolerates the mild heat rise inside the enclosure during summer operation better than PLA (PLA glass-transition is ~60°C, which a wall-mounted enclosure in direct sunlight can approach).

**Acceptable: PLA.** Prints cleanly with minimal warping. Print the back plate and main box in different colors of PLA to reduce sliding friction (dissimilar surface chemistry beats identical). Avoid PLA in enclosures that may see direct sunlight.

**Not recommended: ABS.** Warps too aggressively at the 50 × 80 mm footprint for a hobbyist FDM setup without a heated enclosure.

Print settings (Bambu Studio / PrusaSlicer defaults are fine except where noted):

| Setting | Value |
|---|---|
| Layer height | 0.2 mm |
| Wall count | 4 (for structural integrity around the dovetail slot) |
| Top/bottom layers | 5 |
| Infill | 25% gyroid |
| Supports | None required if oriented correctly |
| First layer speed | Reduce to 50% of default for better bed adhesion |

## PCB mounting

The PCB is retained inside the main box by four M2×6mm screws threading into brass heat-set inserts (M2×4mm insert length) melted into the box's internal standoffs. The standoffs are 6 mm tall, positioning the PCB such that the barrel jack sockets align with the two cutouts on the box's side wall.

Heat-set inserts are strongly preferred over screwing directly into printed plastic — the latter strips after 3–5 removal cycles, while heat-set inserts survive hundreds of cycles.

## Cable management

The barrel jack cables enter through the two side cutouts. The WS2812B matrix cable, when used, exits through a strain-relief slot on the top edge of the main box. This slot is deliberately undersized (~3 mm) relative to the cable's ~5 mm diameter, so the cable snap-fits and does not fall out under its own weight.

The USB-C cable (for programming) enters through a cutout on the top edge, adjacent to the WS2812B slot. For permanent installation, the USB-C cable can be omitted entirely; the board is programmed once and updates OTA.

## Files

STL files for both parts are in this directory (main enclosure repository at `hardware/enclosure/`):

- `back_plate.stl` — the wall-mounted piece
- `main_box.stl` — the PCB housing

Source Fusion 360 files (`.f3d`) are also included for users who want to modify the design. The parametric dimensions in the source files reference a single sketch, so changing the enclosure footprint requires updating one value rather than propagating changes across multiple sketches.

**Note:** STL files are placeholder in this initial commit — will be uploaded once the first physical print is validated for fit against the assembled PCB.
