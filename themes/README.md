# Themes

A theme is a name plus any combination of these files. Nothing is required —
a theme with only a `.css` file simply has no effect on Word output.

```
themes/
├── css/<name>.css            HTML, EPUB, reveal.js and WeasyPrint PDFs
├── typst/<name>.typ          pandoc template used by the Typst PDF engine
└── reference/<name>.docx     --reference-doc for Word
    reference/<name>.odt      --reference-doc for LibreOffice Writer
    reference/<name>.pptx     --reference-doc for PowerPoint
```

Pass `theme=<name>` on a request, or set `DEFAULT_THEME` to make it the default.
`GET /api/themes` lists what the service can see.

Because `themes/` is mounted as a volume, you can add or edit a theme and use it
on the next request — no rebuild, no restart.

## Adding a house style for Word

Pandoc copies fonts, colours, heading styles and page setup out of a reference
document. Generate a starting point, edit it in Word or LibreOffice, and save it
back:

```bash
docker exec markdown-converter pandoc -o /tmp/ref.docx --print-default-data-file reference.docx
docker cp markdown-converter:/tmp/ref.docx themes/reference/house.docx
# open themes/reference/house.docx, restyle "Heading 1", "Body Text", etc., save
```

Then convert with `theme=house`. Only the *styles* are taken from the file — its
text is ignored.

## Adding a CSS theme

Copy `css/default.css` and edit. It is used for HTML output and, more
importantly, for PDFs rendered with the `weasyprint` engine, where you get real
print CSS: `@page` size and margins, running headers and footers,
`target-counter()` for page numbers in the table of contents, and
`break-inside: avoid` to keep tables whole.

## Adding a Typst template

`typst/<name>.typ` is a **pandoc template**, not a plain Typst file — it needs
pandoc's `$body$`, `$if(title)$` and friends. Start from pandoc's own:

```bash
docker exec markdown-converter pandoc --print-default-template=typst > themes/typst/house.typ
```
