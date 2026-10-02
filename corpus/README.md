# The document corpus

The source material the guest assistant retrieves from. Sixteen documents: six brand-wide, five
for Harborview Grand, five for Lakeside Inn.

```
corpus/
  brand/            policies that apply to both properties
  harborview-grand/
  lakeside-inn/
  figures/          generated images          (gitignored)
  pdf/              rendered documents        (gitignored)
```

**Markdown is the source. PDFs are the artifact the pipeline reads.**

```bash
python scripts/make_figures.py     # -> corpus/figures/*.png
python scripts/render_corpus.py    # -> corpus/pdf/*.pdf
```

Both outputs are build artifacts and are not committed. The Markdown is, because a policy change
has to be reviewable as a diff — a 60 KB binary is not.

---

## The rules this corpus is held to

### 1. It is authored *from* the implemented business rules, never independently of them

The cancellation window stated in these documents must be the window
`shared/acceptance-criteria.md` specifies and both backends enforce: **48 hours before check-in,
in the property's own IANA timezone**, with cancellation inside the window allowed but
non-refundable.

This is the corpus's single most important constraint. A document written to *sound* realistic
rather than to *match* produces the worst failure available to this system: an assistant
confidently quoting a policy the API then refuses to honour. Standard RAG metrics would score that
answer well — faithfulness measures agreement with the retrieved document, not with the running
application.

A test asserts the agreement. See `stacks/ai-service/testing-standards.md`, "Policy consistency".

Facts currently pinned to the live system: the 48-hour rule; the 30-night maximum stay; room type
names, codes, nightly rates, occupancies and bed configurations; the seven amenity codes; the eight
rate categories and the fact that only Harborview Grand offers one (AAA/CAA at 10%); and that
**Lakeside Inn has no accessible room type**, which the documents say plainly rather than eliding.

### 2. No fact may exist only in a figure

`pypdf` extracts text, not images. Anything stated only in a picture is invisible to retrieval.

Every figure therefore carries a caption, and the surrounding prose states whatever the figure
shows. The meeting-room layout diagram illustrates a table that already gives the same capacities
in text.

### 3. The two properties are deliberately different

Where a policy could plausibly go either way, the two properties differ — Harborview takes dogs,
Lakeside does not; Harborview's parking is scarce and charged, Lakeside's is free and ample;
Harborview has a 24-hour desk, Lakeside closes at 10:00 PM; Harborview has an accessible room
type, Lakeside has none.

This is a test fixture as much as a content decision. Two near-identical documents that differ in
their particulars are exactly what a retrieval pipeline has to get right, and a corpus where every
property says the same thing would make retrieval look better than it is.

### 4. The renders are realistic on purpose

`render_corpus.py` produces letterhead, running headers, page numbers, justified text, tables and
figures — not a clean single-column render.

Extracting the first version of this corpus surfaced three artefacts that a tidy render would have
hidden entirely, each of which is now a stated ingestion requirement in
`stacks/ai-service/architecture-specification.md`:

- justified text yields **irregular multiple spaces** between words;
- **line wrapping splits phrases**, so substring matching on raw extracted text silently fails;
- **running headers and footers bleed into the text** once per page and will pollute every chunk
  unless stripped.

---

## Adding or changing a document

1. Edit the Markdown, never the PDF.
2. If the change touches a business rule, check it against `shared/acceptance-criteria.md` first.
   If the two disagree, the specification is right.
3. Re-render, re-ingest, and re-run the evaluation suite — a corpus change is a behavioural change
   and is gated like one.
4. Front matter carries `title` and `property`; `property: null` means brand-wide. Ingestion reads
   both into `ai_documents`.
