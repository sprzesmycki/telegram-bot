CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    height_cm REAL,
    weight_kg REAL,
    age INTEGER,
    gender TEXT, -- 'male', 'female'
    activity_level TEXT, -- 'sedentary', 'light', 'moderate', 'active', 'very_active'
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
    daily_calories INTEGER NOT NULL DEFAULT 2000,
    daily_protein_g REAL,
    daily_carbs_g REAL,
    daily_fat_g REAL
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

CREATE TABLE IF NOT EXISTS piano_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    practiced_at DATE NOT NULL,
    duration_minutes INTEGER,
    notes TEXT,
    pieces_practiced TEXT,
    logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS piano_pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    composer TEXT,
    status TEXT NOT NULL DEFAULT 'learning',
    added_at DATE NOT NULL DEFAULT CURRENT_DATE,
    last_practiced_at DATE,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS piano_recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    piece_id INTEGER REFERENCES piano_pieces(id),
    recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT,
    duration_seconds INTEGER,
    feedback_summary TEXT,
    raw_analysis TEXT
);

CREATE TABLE IF NOT EXISTS piano_streak (
    owner_user_id BIGINT PRIMARY KEY,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_practiced_date DATE
);

CREATE TABLE IF NOT EXISTS liquids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    owner_user_id BIGINT NOT NULL,
    drunk_at DATETIME NOT NULL,
    logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT NOT NULL,
    amount_ml INTEGER NOT NULL,
    calories INTEGER DEFAULT 0,
    protein_g REAL DEFAULT 0,
    carbs_g REAL DEFAULT 0,
    fat_g REAL DEFAULT 0,
    raw_llm_response TEXT
);
