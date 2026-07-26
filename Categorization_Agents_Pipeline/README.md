# Categorization_Agents_Pipeline

Reads `{username, bio}` from MinIO, works out what each person is into, and writes
categories + structured tags back to the MinIO table the panels already read.

Internal only. No UI is touched, no external service is called.

```bash
# look first — runs every agent, writes nothing
python -m Categorization_Agents_Pipeline --dry-run --sample 10

# the real run
python -m Categorization_Agents_Pipeline --workers 8

# why did THIS handle get those tags?
python -m Categorization_Agents_Pipeline --explain some_username
```

```python
from Categorization_Agents_Pipeline import CategorizationPipeline
summary = CategorizationPipeline(workers=8).run()
```

## No model, on purpose

There is no LLM here. The agents are a lexicon, a gazetteer and pattern matching,
which buys three things that matter for this job:

* **Reproducible.** The same bio always yields the same verdict, so a re-run after
  a fix is a diff rather than a re-roll.
* **Auditable.** `--explain` shows every signal and its score. A wrong tag is a
  line in `lexicon.py`, not a prompt to re-tune.
* **Fast and free.** ~2,500 profiles/second on one machine, no API, no quota.

The cost is coverage: it knows what the lexicon knows. Extending it is the
intended maintenance path — add terms, get better results, change no code.

## The agents

| # | Agent | Responsibility |
|---|---|---|
| 1 | `DataLoaderAgent` | Read from MinIO, validate, skip empties and scraper artifacts |
| 2 | `BioCleaningAgent` | Normalize text; keep emoji / hashtags / mentions as separate evidence; detect script |
| 3 | `InterestAgent` | Emit a `Signal` per piece of evidence (phrase, word, hashtag, emoji) |
| 4 | `ClassifierAgent` | Turn scored signals into the category set |
| 5 | `EntityAgent` | Named things: locations, sports, teams, athletes, brands, universities, events, jobs, hobbies |
| 6 | `TaggerAgent` | Render tags (`loc:Hunza`, `sport:Cricket`); enforce the location rule |
| 7 | `ConfidenceAgent` | Score each conclusion, apply the threshold |
| 8 | `WriterAgent` | Batch and upsert into `usernames_categories.txt` |

Agents 2–7 are pure functions of their input — no I/O, no shared state — which is
what lets N workers run them at once with no coordination.

## Execution

```
DataLoaderAgent
      │
   ┌──▼──┐
   │queue│
   └──┬──┘
 ┌────┼────┬────┬────┐
 W1   W2   W3   W4  … WN      each: clean → extract → score → classify → tag → submit
 └────┴────┴────┴────┘
      │
  WriterAgent   (buffered, flushed every config.SAVE_EVERY)
```

Honest note on the workers: per-profile work is regex over a short string and
CPython holds the GIL through `re`, so threads here buy pipeline shape and
overlapped store I/O rather than a linear speedup. A whole run is dominated by the
two MinIO round-trips at either end.

## Order is load-bearing

Extraction → **overlap suppression** → inference → scoring. Suppression drops any
match sitting inside a longer one, which is what stops:

| Bio text | Without suppression | With |
|---|---|---|
| `Real Madrid` | `loc:Madrid` + `team:Real Madrid` | `team:Real Madrid` |
| `makeup artist` | `Art` + `job:Makeup Artist` | `job:Makeup Artist`, Beauty |
| `personal trainer` | `Education` (from "trainer") | `Fitness`, `job:Personal Trainer` |

Inference runs *after* suppression so a phantom entity can't imply a category.

## The location rule

Any place named yields **both** tags, as specified:

```
"Exploring Hunza & Skardu"      -> Travel, loc:Hunza, loc:Skardu
"Italy, France and Switzerland" -> Travel, loc:France, loc:Italy, loc:Switzerland
```

`TaggerAgent` enforces it absolutely — even if the implied Travel signal were cut
by the confidence threshold, a surviving `loc:` tag re-adds it.

## Output

Written to `usernames_categories.txt` in MinIO (tab-separated, one row per handle):

| column | |
|---|---|
| `username` | |
| `bio` | |
| `category` | the single best category — **unchanged shape**, so the existing panels keep working |
| `categories` | the full list |
| `tags` | `Travel; loc:Hunza; sport:Cricket; …` |
| `specifics`, `locations` | kept for the existing Data-panel columns |
| `used_count` | campaign usage; **never** reset by a re-run |

## Tuning

Everything adjustable is in `config.py`: `MIN_CONFIDENCE` (the cut),
`WEIGHT` (how much each kind of evidence is worth), `AGREEMENT_BONUS`
(what independent corroboration adds), worker count, batch size, caps.

Scoring: a conclusion starts at its strongest signal's weight, then gains
`AGREEMENT_BONUS` per additional *different kind* of evidence. Repeating the same
kind adds nothing — saying "travel" twice isn't stronger than saying it once,
but "travel" + ✈️ + naming Skardu is.

## Extending

Almost always `lexicon.py`:

* a category term → `CATEGORY_TERMS`
* a place → the `_places(...)` calls (yields `loc:` + Travel automatically)
* a sport / team / athlete / brand / university → the matching dict
* a new facet → add the dict, list it in `_GAZETTEERS`, and map it in
  `FACET_CATEGORY` if it should imply a category

Multi-word entries are matched as phrases and score higher than single words,
and they win over any shorter term inside them.
