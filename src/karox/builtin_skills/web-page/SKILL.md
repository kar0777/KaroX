---
name: web-page
version: 1.0.0
description: Build or change a web page or small browser app without breaking what is there.
when_to_use: HTML/CSS/JS pages, landing pages, small UIs, dashboards, forms.
triggers: ["*.html", "*.css", "*.jsx", "*.tsx"]
---
# Web page recipe
- Read the existing page structure, styles and scripts before adding anything; follow its naming, colors, spacing and component patterns.
- Small request, small change: edit only the element asked about. Do not restyle other parts.
- Layout with flex or grid and gap, not margins between siblings. Make it work at phone width (no horizontal scroll) and at desktop width.
- Semantic HTML: buttons are <button>, links are <a>, inputs have labels. Keep text readable (contrast, at least 14px body text).
- No placeholder filler: no lorem ipsum, fake stats or decorative sections nobody asked for.
- Wire every interactive element; no dead buttons. Handle empty and error states for data.
- Load only libraries the task allows. No external images or fonts unless allowed.
- Before you finish: no console errors; the page loads from a clean start; if visual_check is offered, call it.
