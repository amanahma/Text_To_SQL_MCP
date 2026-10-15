-- QueryGuard demo database — SCHEMA (synthetic, authored by us; NOT derived from BIRD).
--
-- A tiny fictional bookstore. Three related tables. The customers table has two
-- fake-PII columns (email, phone) so the PII-masking path can be exercised:
--   * email  -> fake addresses at example.com (a reserved documentation domain)
--   * phone  -> fake numbers in the 555-01xx range (reserved for fiction)
-- All data is invented. No real person, address, phone or email appears here.
--
-- Idempotent: safe to run repeatedly (drops and recreates). Pure SQL; the loader
-- (scripts/setup_demo.py) and the postgres initdb mechanism both just run this file.

\set ON_ERROR_STOP on

DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS books CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    full_name   text NOT NULL,
    email       text NOT NULL,   -- fake PII (masked by column name: strong token "email")
    phone       text NOT NULL,   -- fake PII (masked by column name: strong token "phone")
    city        text NOT NULL
);
COMMENT ON TABLE customers IS 'Fictional bookstore customers (synthetic demo data).';
COMMENT ON COLUMN customers.email IS 'Fake email at example.com; masked by QueryGuard PII rule.';

CREATE TABLE books (
    book_id integer PRIMARY KEY,
    title   text NOT NULL,
    author  text NOT NULL,
    genre   text NOT NULL,
    price   numeric(6, 2) NOT NULL
);
COMMENT ON TABLE books IS 'Fictional book catalogue (synthetic demo data).';

CREATE TABLE orders (
    order_id    integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers(customer_id),
    book_id     integer NOT NULL REFERENCES books(book_id),
    quantity    integer NOT NULL CHECK (quantity > 0),
    order_date  date NOT NULL
);
COMMENT ON TABLE orders IS 'Fictional orders linking customers to books (synthetic demo data).';
