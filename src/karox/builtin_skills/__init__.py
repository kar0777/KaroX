"""Skill recipes that ship inside KaroX.

Read from the installed package, so a wheel carries them: the catalogue in the
system prompt advertises these by name, and ``skill.load`` reads them on demand.
A wheel that shipped the catalogue but not the recipes would name skills that
cannot be opened.

They are ordinary Skills in every other respect -- same frontmatter, same
validation, same confinement, same ``source_kind="builtin"`` precedence below
the project and the user -- because a recipe that bypassed the loader's checks
would be the one file in the product nobody validated.
"""
