# Gold answer issues (BIRD Mini-Dev, PostgreSQL)

These questions stay in every accuracy number. They are reported as group D in the error analysis.

## Q37 (california_schools): gold picks a school with no rate; 18-way tie
- Gold: ORDER BY NumGE1500 / NULLIF(NumTstTakr, 0) ASC NULLS FIRST LIMIT 1.
- Gold's answer (2125 Jefferson Avenue) has NumTstTakr = 0 and NumGE1500 = NULL, so its rate is undefined; NULLS FIRST puts it on top.
- Among schools with a defined rate, 18 tie at rate 0, so the question has no single correct answer; LIMIT 1 picks one arbitrarily.
- The model excluded zero-taker schools and returned one of the 18 tied schools: a defensible answer, scored wrong.
- Verified 2026-10-04 with two direct queries on california_schools.

## Q83 (california_schools): two questions in one
- The question asks for a count AND a per-city list; gold answers only a filtered per-city list.

## Q247 (toxicology, dev set): gold answers a different question than the hint
- Hint: "atoms cannot bond with other atoms means atom_id NOT in connected table" (atom-level).
- Gold excludes elements that appear in ANY connected atom (element-level) → 3 elements (k, zn, na).
- Model followed the hint literally (atoms not in connected, checking both atom_id and atom_id2) → 5 elements (cl, h, k, na, zn).
- The model's reading matches the hint more closely; scored wrong.