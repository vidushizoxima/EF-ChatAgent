# Knowledge base

Drop markdown files in this folder. `search_knowledge` reads them, so anything
written here is something the agent can say on a call.

## Format

Split each file into `##` sections. The heading is the retrieval key — write it
the way a customer would ask about it, not the way a catalogue would title it.

```markdown
## AMC plan pricing for water purifiers
The Basic plan is two thousand four hundred rupees a year and covers two
scheduled services plus unlimited breakdown visits. Labour is included, spare
parts are charged separately.

## RO purifier not dispensing water
First check that the tap is on and the tank is not empty...
```

## Rules that matter

- **Write numbers as words.** "two thousand four hundred rupees", not "Rs 2,400".
  The agent reads these out loud.
- **Do not use numbered or bulleted lists.** They get flattened into prose before
  being spoken anyway, and flattening loses meaning. Write the prose yourself.
- **One topic per section.** Retrieval is per section, so a section covering three
  topics will be pulled in for all three and the agent will over-answer.
- **Keep sections short.** Two to four sentences. A voice answer should fit in one
  or two spoken turns.
- Prices, warranty terms and SLAs live HERE and nowhere else. The prompt explicitly
  forbids the agent from quoting these from memory, so if it isn't in this folder,
  the agent will say it needs to check — which is the correct behaviour.

Files are re-read every 60 seconds, so edits show up on the next call without a restart.

## Suggested files

| File | Covers |
|---|---|
| `products-water-purifiers.md` | Models, capacities, TDS suitability, prices |
| `products-air-purifiers.md`   | Models, room sizes, filter types |
| `products-vacuum-cleaners.md` | Models, use cases |
| `amc-plans.md`                | AMC/CMC tiers, pricing, inclusions, exclusions |
| `warranty-and-service.md`     | Warranty terms, service SLAs, visit charges |
| `troubleshooting.md`          | Safe first-line checks per product category |
| `consumables.md`              | Filter change schedules, consumable prices |

## Outgrown this?

Set `KB_BACKEND=pgvector` and populate the vector table — the agent-side interface
is identical, so nothing else changes.
