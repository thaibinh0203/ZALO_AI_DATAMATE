-- SYNTHETIC B1 ONLY: not a competition schema. Each sales row is one sale line.
CREATE TABLE products (
    product_id TEXT PRIMARY KEY,
    product_name TEXT,
    unit TEXT
);
CREATE TABLE sales (
    sale_id TEXT PRIMARY KEY,
    sale_date DATE,
    product_id TEXT REFERENCES products(product_id),
    quantity INTEGER,
    amount_vnd BIGINT,
    status TEXT
);
