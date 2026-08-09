# Pipeline: Shopping

**Status:** implemented
**Category routed:** `Shopping`

---

## 1. What it does

You say what you need, the way you would say it to a person. The thought is filed as
usual, and a moment later the things to buy have been pulled out of it and put on a
list you can tick off with one tap while standing in a shop.

```
you:  need bananas and 2L oat milk
vos:  ✅ Filed under Shopping · 🔗 bananas · oat milk
vos:  🛒 Added to your list
      • bananas
      • oat milk · 2L

you:  /shopping
vos:  🛒 Shopping list

      1. bananas
      2. oat milk · 2L
      [✓ bananas] [✓ oat milk]
```

Tapping a button ticks the item off, edits the list in place, and adds an `↩ Undo last`
row — because the failure mode of a keyboard full of adjacent buttons is the wrong tap,
and the correction has to be as cheap as the mistake.

## 2. Why it is more than a text field

The list is derived from thoughts you were going to capture anyway. There is no "add
item" mode to enter and no syntax to remember, so the cost of putting something on the
list is the cost of mentioning it. That only works if extraction is trustworthy in one
specific direction: it must not invent items. An item you never asked for, discovered in
a shop, is indistinguishable from your own forgetfulness.

## 3. Where the state lives

Nowhere new is the goal, but the shopping list genuinely does not fit the graph, so it
gets a small store of its own (ADR-012).

| | |
|---|---|
| **Journal** | `capture` lines, as always, plus `item_mark` lines for every tick and un-tick (ADR-013). Source of truth. |
| **SQLite** (`shopping.db`) | `items`, `adds`, `extractions`. A projection — disposable, rebuilt from the journal. |
| **Neo4j** | Nothing shopping-specific. The entities in a shopping thought land there through ordinary classification, exactly as they did before this pipeline existed. |

The graph keeps no list state at all. A shopping list is a handful of rows that flip
between two values and get read back in one order; as nodes it would be volume without
a traversal that benefits from it.

## 4. Pipeline shape

```
capture → classify → (category == Shopping) → queue → extract → project → reply
```

One LangGraph node, `extract`, with `ShoppingExtraction` as structured output — the same
shape as `analyze`, and queued on the same single worker as video distillation because
it is another model call and the polling loop must not wait for it.

Extraction is a **separate call** from classification, deliberately. Classification
already produces entities, but those are tuned for findability: "the oat milk I bought
was watery" should absolutely be findable under oat milk, and should just as absolutely
not put oat milk on the list. Those are different questions, and folding the second into
`SYSTEM_PROMPT` would tie every category's classification quality to this one feature.

## 5. Identity and the time rule

Items dedupe on `canonical()` — the same case- and whitespace-insensitive key entity
resolution uses. Mentioning oat milk in two thoughts produces one row with two `adds`
rows behind it, and the most recent request supplies the quantity.

Every state change is gated on a timestamp, which makes the events commutative:

- An `add` re-opens a bought item only when the capture is **newer** than the purchase.
  Buying milk on Monday and asking for milk on Friday is a new request; replaying
  Monday's thought during a rebuild is not.
- A `mark` applies only when it is **newer** than whatever last decided that item.

So the journal can be replayed in any order and converge — which is what lets startup
recovery, out-of-order re-extraction, and `--rebuild` share one code path with no
sequencer. It is the same property `Neo4jGraph._link_thread` was written for.

## 6. Failure modes

| What happens | What the user sees | What the system does |
|---|---|---|
| Model call fails | "Couldn't turn that into a list", plus the thought is safe | No success marker written, so the next restart retries |
| Model returns nothing to buy | "Nothing to add to the list from that one" | Marked extracted; never retried |
| Journal write fails on a tick | An alert on the tap, nothing changes | The list is untouched — a tick that was not recorded must not look like it happened |
| Store write fails after journalling | The tick appears to do nothing until restart | Startup replay applies it |
| Button tapped from a stale list | "That list is out of date — send /shopping again" | Refused; the callback carries a digest of the item name, so a reused row id cannot be ticked by accident |
| Daily budget exhausted | Nothing (the capture reply already said so) | Extraction is skipped and retried on the next restart |

Unlike video, there is **no permanence flag**. A video can lose its captions forever, so
retrying it forever is waste; every failure here is a model or network failure, and one
cheap retry per restart is bounded.

## 7. Commands

| Command | Does |
|---|---|
| *(just talk)* | "out of coffee, and 2L oat milk" — items are extracted automatically |
| `/shopping` | The list, numbered, with a tap-to-buy button per item |
| `/bought <name\|number>` | Tick something off by typing — for when the list has scrolled away |

There is no `/unbought`. Undo is a button on the list, because the mistake it corrects is
a mis-tap, and making the fix a typed command would be slower than the error.

## 8. Decisions

- **The list is SQLite, not the graph** (ADR-012). Keeps the graph about thinking.
- **Ticks are journalled** (ADR-013). Bought-ness is a decision you made, not something
  a model derived. Without a journal line, `vos reclassify --rebuild` — an ordinary
  operation here — would silently reset the list to pending, and you would find out in
  a shop.
- **Items are re-extracted after a rebuild, ticks are not.** Ticks replay instantly
  because they only exist in the journal. Items cost a model call each, so the emptied
  `extractions` table is left to say "nothing extracted yet" and the next startup does
  the work in the background.
- **Buttons carry a name digest, not just a row id.** A rebuild reassigns row ids; the
  digest turns a stale button into a refusal instead of a wrong purchase.
- **Extraction never invents.** The prompt's first rule is that an empty list is a
  correct answer, with three of its four examples extracting nothing.

## 9. Scope boundary

Not in scope, and not accidentally half-built: reminders or location triggers, shop or
price integrations, quantities as structured numbers (they are free text — "a dozen" is
a real answer), recipe expansion, and sharing a list with another person. Each would
need its own design; none is blocked by anything here.
