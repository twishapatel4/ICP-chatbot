# titles.json

Job titles come from three places, each title listed once:

- A hand-written list of common LinkedIn titles in `scripts/linkedin_extra_titles.txt`.
- O*NET (US Department of Labor, https://www.onetcenter.org), © its authors, CC BY 4.0: the
  occupation titles and the alternate / reported titles from the O*NET database.
- ESCO, the European Skills, Competences, Qualifications and Occupations classification,
  © European Union, https://esco.ec.europa.eu, reused under the ESCO terms of use (free reuse
  with attribution): each occupation's preferred English name and English alternative labels.

Rebuild with `python scripts/sync_esco_titles.py` (ESCO, then merges the rest), or
`python scripts/sync_esco_titles.py --extras-only` to re-merge without calling ESCO.
