CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    UNIQUE(owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS active_profile (
    user_id BIGINT PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id)
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    owner_user_id BIGINT NOT NULL,
    eaten_at DATETIME NOT NULL,
    logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT NOT NULL,
    calories INTEGER,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    raw_llm_response TEXT,
    photo_path TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id),
    daily_calories INTEGER NOT NULL DEFAULT 2000
);

CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    reminder_time TEXT NOT NULL,
    dose TEXT,
    active BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS supplement_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplement_id INTEGER NOT NULL REFERENCES supplements(id),
    profile_id INTEGER NOT NULL,
    taken_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
