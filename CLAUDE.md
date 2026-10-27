# Working on this repository

## Dashboard UI

The dashboard follows one design, described in [docs/design/README.md](docs/design/README.md)
with approved wireframes in [docs/design/wireframes/](docs/design/wireframes/).

* **Build every page to its wireframe** and to the rules in `docs/design/README.md` (navigation,
  page layouts, drawer editing, one save model, live sections, tokens, status vocabulary). Do not
  invent a new pattern for a page that has one.
* **A new page, or a big change to an existing one, needs a wireframe first.** Draw it, get the
  user's approval, and only then build it. Add the approved wireframe (source `.dc.html`, rendered
  HTML and PNG) to `docs/design/wireframes/` and a row in the README's wireframe table, in the same
  pull request as the code or before it. Small changes inside an existing pattern (a new column,
  a new field in a drawer) need no wireframe.
* **Wireframes in the repo use example data only.** The repository is public: no real hostnames,
  domains, IP addresses, node or user names. Use `*.example.lan`, `10.0.x.x`, `node-a/b/c`,
  `admin`.
* **When code and a wireframe disagree,** fix the code, or update the wireframe in the same pull
  request and say why.
