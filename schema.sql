CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
    category TEXT NOT NULL CHECK (
        (kind = 'income' AND category IN ('Жалақы', 'Қосымша табыс', 'Басқа кіріс')) OR
        (kind = 'expense' AND category IN ('Азық-түлік', 'Көлік', 'Коммуналдық', 'Демалыс', 'Басқа шығыс'))
    ),
    amount_minor INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer' AND amount_minor BETWEEN 1 AND 100000000000),
    date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 120),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One shared household account; no per-user ownership in this classroom MVP.
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
