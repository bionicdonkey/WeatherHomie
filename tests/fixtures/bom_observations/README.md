`IDN60801.99999.synthetic.json` is invented data in the shape of a BOM observations product (including its quirks: "-"
placeholders, nulls, CALM wind). Real downloads, if you want to test against them, go in the git-ignored `real/` folder:
`curl -A "<identifying agent>" -o real/IDN60801.94768.json https://reg.bom.gov.au/fwo/IDN60801/IDN60801.94768.json`
(BOM web products are covered by BOM's copyright notice, so they are not committed).
