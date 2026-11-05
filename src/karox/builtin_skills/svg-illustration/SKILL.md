---
name: svg-illustration
version: 1.0.0
description: Draw an illustration, icon, logo or diagram as a standalone SVG file.
when_to_use: drawings, illustrations, cartoons, icons, logos, badges or diagrams saved as SVG.
triggers: [svg, illustration, drawing]
---
# SVG illustration recipe

Scope discipline
- This is a technique guide, not a stored picture. Draw what the task describes; do not reuse a layout, palette or character from another task.

File
- One self-contained file: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 W H" width="W" height="H">` with a `<title>` naming the picture. No external images, fonts or scripts.
- Keep it well-formed XML: escape `&` and `<` in text, close every element. A browser shows nothing for a broken file.

Plan before drawing
- Pick the canvas (800x600 is a good default), the ground line and the subject's box: centred, about 60-75% of the width, nothing cut off.
- Work out the key points first -- joints, centres, contact points -- and derive every connected part from those numbers, so pieces that touch meet exactly instead of almost.
- Order layers back to front: backdrop, distant scenery, ground, cast shadow, parts behind the subject, the subject, parts in front of it. A part that passes in front (a limb, a handle, a strap) comes later in the file.

Recognizable subjects
- Each named subject is recognizable at first glance by its signature features; exaggerate them a little (a characteristic shape, colour or proportion) rather than drawing a generic body.
- When one subject uses another (rides, holds, sits on, pushes), draw the contacts where they really are: the body rests on what carries it, hands or their equivalent hold what they steer, feet reach what they push or stand on. Bent joints make a pose read as a pose; straight sticks do not.
- A built object keeps the structure that makes it work: the parts a viewer expects are present and joined where they join in reality, with real-world proportions. Nothing floats, hangs disconnected or passes through another part.

Craft
- A small harmonious palette with two or three tones per material: base fill, a darker side away from the light, a highlight. Linear and radial gradients give volume cheaply.
- Consistent outlines (one or two stroke widths, round caps and joins) and smooth curves (`C`/`Q` path commands) instead of jagged polygons.
- A soft shadow under the subject, a backdrop gradient and a few simple scenery shapes give it a place; a slight lean or motion lines show movement when the subject moves.
- Small details at the viewing size make it look finished -- an eye highlight, a texture line, a fastening -- without burying the main shapes.

Before you finish
- Render it with visual.check and look at it as a viewer would: is every named subject recognizable, do contacts and connections look right, is anything floating, cut off, hidden by a later layer or out of order? Fix what you see with targeted edits, then look again.
- A pass that still lists notes is a first draft: spend one round of targeted edits on the notes a viewer would notice (a joint that does not meet, a part drawn in the wrong layer, a proportion that looks off), then look once more.
