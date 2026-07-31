# UX bug inventory

Defects in the terminal client, each with the command that reproduces it and the
test that proves it. Opened while establishing the 5.0 baseline, after the user
report "text selection works incorrectly, and there are many bugs like it".

## How this list works

Every entry has an executable test. Most are marked `unittest.expectedFailure`
with the entry's identifier in a comment, which has two consequences worth being
explicit about:

- the suite stays green while the defect is open, so an unrelated change is not
  blocked by a known bug;
- unittest reports an **unexpected success as a failure**, so the moment a defect
  is fixed the run goes red until the decorator is deleted. A bug here cannot be
  quietly forgotten, and cannot be quietly reintroduced afterwards.

Close an entry by deleting the decorator in the same commit as the fix, and moving
the row to *Closed* with the commit that did it. Do not delete an entry — a fixed
defect with a live test is the only thing that stops it coming back.

Reproduce any of these with:

```bash
python -m unittest discover -s tests -p "test_tui_selection.py" -v
python -m unittest discover -s tests -p "test_tui_layout.py" -v
```

## Open

| ID | P | Area | Symptom |
|---|---|---|---|
| [UX-006](#ux-006) | P1 | copy | No way to hand the mouse back to the terminal's own selection |
| [UX-009](#ux-009) | P2 | layout | Sponsor ticker starts mid-word |
| [UX-010](#ux-010) | P1 | layout | A 14-row window leaves the conversation two rows |
| [UX-011](#ux-011) | P1 | layout | The welcome's first line is unreachable at that size |
| [UX-012](#ux-012) | P1 | layout | Status columns run into each other at 80 columns |
| [UX-013](#ux-013) | P1 | layout | Status values are truncated with no marker |

## Closed

Nine of the fifteen, and eight of them by one change: the transcript is now a
scroll container of one widget per message instead of a `RichLog` with selection
written by hand. Deleting the hand-written layer is what closed them, rather than
nine separate repairs.

| ID | P | Closed by | Now asserted by |
|---|---|---|---|
| UX-001 | P0 | native character-level selection | `test_selecting_four_characters_copies_four_characters` |
| UX-002 | P0 | Textual owns the drag | `test_a_real_drag_selects_from_the_press_to_the_release` |
| UX-003 | P0 | the height table is gone entirely | `test_pointing_at_a_row_returns_the_message_drawn_on_it` |
| UX-004 | P2 | `render_line` override deleted | — (closed by deletion) |
| UX-005 | P0 | Ctrl+C copies, Esc stops | `test_ctrl_c_does_not_stop_a_running_agent` |
| UX-007 | P1 | a lookup failure is reported | `action_copy_selection` notifies with severity |
| UX-008 | P1 | the notice says which copy happened | `test_ctrl_c_with_nothing_selected_copies_the_last_answer` |
| UX-014 | P1 | CSS frame at `width: 1fr` | `test_an_answer_uses_the_width_of_a_wide_window` |
| UX-015 | P1 | a widget tree is laid out again | `test_an_answer_is_relaid_out_when_the_window_changes` |

Measured after the change, at a 116-column conversation: an answer occupies 72
columns rather than 37, selecting `BETA` out of `ALPHA BETA GAMMA` yields `BETA`,
and a fenced code block copies with its indentation intact -- which the previous
implementation could not do at all, because a Rich renderable returns nothing from
`Widget.get_selection`.

What it cost: `src/karox/markdown_render.py` and its fourteen tests were deleted.
A Rich renderable cannot be selected, so the custom markdown renderer could not
stay and the answer be copyable; Textual's `Markdown` widget renders each block as
a `Static` holding `Content`, which is why the code block now copies.
`markdown-it-py` and Pygments are still required -- Textual's widget parses and
highlights through them -- and are declared for that reason in
`scripts/check_dependencies.py`.

---

## Root cause behind most of this

`ChatLog` extends `RichLog`, and `RichLog` does not take part in Textual's
selection machinery: it renders no highlight and its `get_selection` yields
nothing. In Textual 6.12 native character-level selection works for widgets that
render a `Visual` — `Static`, `Label`, `Markdown` — and `RichLog` is not one.

So `ChatLog` implements selection by hand: mouse handlers that build a `Selection`
from line numbers, a `render_line` override that repaints whole rows, and a
`get_selection` that maps a vertical range onto a list of whole messages through a
table of heights snapshotted at write time. UX-001 through UX-004 and UX-007 are
all consequences of that one decision rather than independent mistakes, which is
why they are fixed by moving the transcript onto widgets that select natively, not
by repairing the mapping.

The existing tests did not catch any of it, and that is itself the finding.
`test_chatlog_renders_selection_highlight` asserts that a highlight exists and
that the copied text contains `"KaroX"`; `test_chatlog_mouse_drag_selects_and_
copies_chat_lines` asserts the clipboard contains `"KaroX"` and not the previous
answer. Both pass while the feature returns the wrong text, because neither ever
asserts *which* text. A test that cannot fail when the behaviour is wrong is worse
than no test: it converts an open question into a settled one.

---

### UX-001
**P0 · selection · `get_selection` returns whole messages**

Select the word `BETA` out of an answer reading `ALPHA BETA GAMMA` and press copy.
The clipboard contains `ALPHA BETA GAMMA`.

`get_selection` reads only `selection.start.y` and `selection.end.y`, maps them to
indices in `self._plain_lines`, and returns `"\n".join(lines[start:end + 1])`.
Entries in that list are whole messages, so the smallest unit that can be copied
is one message. A three-line answer with one interesting line copies all three.

Expected: the copied text is exactly the selected range, to the character.

Proven by `test_selecting_four_characters_copies_four_characters` and
`test_selecting_one_line_of_a_message_copies_one_line`.

### UX-002
**P0 · selection · the drag has no horizontal axis**

`_begin_drag` anchors the selection at `Offset(0, line)` and `_extend_drag` ends it
at `Offset(10_000, end)`. The `x` of the mouse event is read nowhere, so no drag a
user can perform expresses a sub-line range — the anchor is column 0 and the end is
past the right edge by construction.

Listed separately from UX-001 because they are different code paths: UX-001 makes a
precise selection impossible to *read*, UX-002 makes it impossible to *express*.
Fixing either alone changes nothing.

Expected: a drag selects from the character under the press to the character under
the release.

Covered by the UX-001 tests, which set the selection directly and so demonstrate
that even a correct one is read wrongly.

### UX-003
**P0 · selection · the row-to-message table is fabricated**

Measured on an 80×24 window with a normal conversation:

```
[0] end_height=1   'KaroX готов.\nНапишите задачу…\nВведите /…'   ← draws 3 rows
[1] end_height=6   'первая задача'
[2] end_height=9   'ALPHA BETA GAMMA'
```

Entry 0 occupies rows 0, 1 and 2 and is recorded as ending at row 1. Entry 1's
block therefore begins two rows early and covers rows 1–5, of which 1 and 2 are
still the welcome. **Selecting the end of the welcome copies the first user
message.**

The cause is documented in `RichLog.write`'s own docstring: rendering is deferred
until the widget's size is known, and a write issued from `compose` or `on_mount`
is not rendered immediately. The welcome is written exactly there, so
`ChatLog.write` reads `virtual_size.height == 0` right afterwards and the
`max(total, previous + 1)` fallback invents a height of 1. The table is
append-only, so nothing repairs it later.

Expected: the row a user points at resolves to the message drawn on that row.

Proven by `test_the_recorded_height_of_a_message_matches_what_it_renders`,
`test_selecting_the_welcome_does_not_return_a_later_message`, and
`test_a_message_written_before_the_size_is_known_records_a_real_height`.

### UX-004
**P2 · selection · the highlight depends on a private attribute**

`render_line` rebuilds the strip from `strip._segments`, a private attribute, inside
a bare `except Exception: pass`. A Textual upgrade that renames it turns the
selection highlight off silently — no error, no log line, just a selection the user
can no longer see. Two existing tests read `strip._segments` as well, so they would
break at the same moment and for the same reason rather than catching it.

It also forces the selection style onto every segment, discarding the syntax
highlighting inside the selected region.

Expected: highlighting uses the documented selection API, and a failure to
highlight is reported rather than swallowed.

No test: this one is closed by deleting the code in question, and a test for a
private attribute would have to be deleted with it.

### UX-005
**P0 · copy · Ctrl+C aborts the task instead of copying**

While a task is running, select any text and press Ctrl+C. The task stops and
nothing is copied.

`action_stop_or_copy` begins `if self.agent_busy: self.action_stop_agent(); return`.
There is no second binding that copies, so during the one period a user most wants
to copy something — a path or an error scrolling past while work is in progress —
the copy key destroys the work instead. Esc is already bound to stop, so the
overload buys nothing.

Expected: copy and stop are separate keys. Copy works whether or not a task is
running.

Proven by `test_copying_is_possible_while_a_task_is_running`.

### UX-006
**P1 · copy · no way to hand the mouse back to the terminal**

Originally reported as `_begin_drag` calling `capture_mouse()`. That call is gone
with the hand-written selection, but the underlying gap is not: while the
application is reading mouse events, the terminal emulator's own selection is
unavailable, and with it the copy integration a user relies on over SSH and inside
tmux. Copying from inside KaroX now works through OSC 52, which covers most of
that, but a user whose terminal already solves selection has no way to ask KaroX to
stop competing for the mouse.

Expected: a binding that releases the mouse, and a note in the help overlay saying
so.

No test yet: the fix is a binding, and the test belongs with it.

### UX-007
**P1 · selection · a failed lookup is indistinguishable from an empty one**

`get_selection` wraps its whole body in `except Exception: return None`, and the
caller treats `None` as "nothing selected". Any internal failure therefore presents
as an empty selection, and combined with UX-008 the user is told "Copied" and
receives a different message.

Expected: a lookup failure is reported rather than disguised as a user action.

### UX-008
**P1 · copy · a fallback copy is announced like a selection copy**

With nothing selected, `action_stop_or_copy` copies `_last_assistant_content` and
shows the same "Скопировано" notice as a real selection copy. Taken with UX-001,
UX-003 and UX-007 — all of which can make a genuine selection read as empty — a
user who selects a line, presses copy, and is told it worked can be holding an
entirely different message with no way to notice.

Expected: the two cases are distinguishable, and a fallback says what it copied.

Proven by
`test_copying_the_last_answer_is_distinguishable_from_copying_a_selection`.

### UX-009
**P2 · layout · the sponsor ticker starts mid-word**

At 80 columns the first render reads `асибо нашим партнёрам: routing.run — …`. The
scrolling offset starts inside the first word, so the initial frame shows a
fragment. Cosmetic, but it is on the first screen a new user sees.

Expected: the ticker begins at a word boundary.

### UX-010
**P1 · layout · a 14-row window leaves the conversation two rows**

With the product's default settings at 46×14, `#conversation` is two rows tall —
14% of the window. Brand, ticker, status bar, two separators and the composer take
a fixed twelve rows no matter how few there are to divide.

The sponsor ticker is on by default and costs exactly the row that makes the
difference: with it off the conversation gets four rows. An existing test pins the
share at a 24-row window, which is above where the guarantee stops holding.

Expected: the conversation keeps a usable share at any size the application agrees
to run at, or says the window is too small.

Proven by `test_a_narrow_window_gives_the_chat_a_usable_share`.

### UX-011
**P1 · layout · the welcome's first line is unreachable**

Consequence of UX-010, listed separately because it is what the user actually
experiences. Two rows cannot hold the three-line welcome, and the log has already
scrolled to the bottom, so `KaroX готов.` — the line stating that the product works
at all — is the one line never shown, with no scrollbar or marker to suggest
anything is above.

Expected: the message shown before the user has typed anything is readable in full.

Proven by `test_the_welcome_is_fully_visible_when_it_first_appears`.

### UX-012
**P1 · layout · status columns run into each other**

At 80 columns the status row reads:

```
репозиторий:   модель:        сессия: новая  контекст: лимитмост: выключен
```

`#status` is a `Horizontal` of five `Static` widgets at `width: 1fr`, so each gets
15 cells and there is no gutter. `контекст: лимит` is exactly 15 characters, fills
its column, and the next begins immediately. The result reads as a rendering fault
rather than as two fields — at the most common terminal width there is.

Expected: adjacent fields are separated whatever their content.

Proven by `test_status_columns_do_not_run_into_each_other`.

### UX-013
**P1 · layout · status values are truncated with no marker**

At 46 columns each column gets 8 cells: `openai/model-a` is drawn as `openai/m`,
and the repository label breaks across two rows mid-word as `репозито` / `рий:`. A
value cut with no ellipsis reads as a different value, and the person checking
which model is selected is exactly the one who cannot afford that.

Expected: a truncated value is marked as truncated, or the row reflows to fewer
fields rather than narrower ones.

Proven by `test_a_truncated_status_value_says_it_was_truncated`.

### UX-014
**P1 · layout · an answer uses a third of a wide window**

At a 120-column window the conversation area is 116 columns and the answer panel is
drawn 37 columns wide, wrapping the text into eight lines and leaving 79 columns
empty.

`RichLog.write` takes `expand=False` by default and the transcript never overrides
it, so a renderable is laid out at its own measured width while the content region
serves only as an upper bound. A wide terminal reads like a phone.

Expected: an answer uses the width available to it.

Proven by `test_an_answer_uses_the_width_of_a_wide_window`.

### UX-015
**P1 · layout · nothing is re-wrapped on resize**

`RichLog` renders each write once into a list of lines and keeps them. Measured: an
answer written at 56 columns occupies the same rows after the window is widened to
140, and one written wide keeps its recorded width when the window shrinks below
it.

Expected: resizing the window re-lays out the conversation.

Proven by `test_an_answer_is_relaid_out_when_the_window_changes`.
