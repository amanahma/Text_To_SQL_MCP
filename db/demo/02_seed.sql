-- QueryGuard demo database — SEED DATA (synthetic, deterministic, authored by us).
--
-- Row totals (all well under the 500-row cap for the demo):
--   customers  40
--   books      30
--   orders    200
-- Everything is generated from generate_series so the data is reproducible and
-- obviously fake. Emails are <name>@example.com; phones are 555-01NN.

\set ON_ERROR_STOP on

TRUNCATE orders, books, customers RESTART IDENTITY CASCADE;

-- 40 customers. email + phone are fake PII.
INSERT INTO customers (customer_id, full_name, email, phone, city)
SELECT
    g,
    'Customer ' || g,
    'customer' || g || '@example.com',
    '555-01' || lpad((g % 100)::text, 2, '0'),
    (ARRAY['Springfield', 'Rivertown', 'Lakeview', 'Hillcrest', 'Fairview'])[1 + (g % 5)]
FROM generate_series(1, 40) AS g;

-- 30 books.
INSERT INTO books (book_id, title, author, genre, price)
SELECT
    g,
    'Book Title ' || g,
    'Author ' || (1 + (g % 12)),
    (ARRAY['Fiction', 'Nonfiction', 'Science', 'History', 'Poetry', 'Mystery'])[1 + (g % 6)],
    round((5 + (g * 1.37))::numeric, 2)
FROM generate_series(1, 30) AS g;

-- 200 orders, each referencing an existing customer and book.
INSERT INTO orders (order_id, customer_id, book_id, quantity, order_date)
SELECT
    g,
    1 + (g % 40),
    1 + (g % 30),
    1 + (g % 5),
    DATE '2025-01-01' + ((g * 2) % 300)
FROM generate_series(1, 200) AS g;
