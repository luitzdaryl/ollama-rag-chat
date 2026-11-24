-- 002_accounts.sql: users, sessions and one-time setup links

CREATE TABLE users (
    id             bigserial PRIMARY KEY,
    username       text NOT NULL,
    display_name   text NOT NULL,
    email          text,                      -- optional contact address (not verified, not used yet)
    password_hash  text,                      -- NULL until the person sets a password via a setup link
    role           text NOT NULL DEFAULT 'user'   CHECK (role IN ('admin', 'user')),
    status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'pending')),  -- 'pending' is reserved for a future approval flow
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at  timestamptz
);
CREATE UNIQUE INDEX users_username_lower_key ON users (lower(username));

CREATE TABLE sessions (
    token_hash    text PRIMARY KEY,           -- SHA-256 of the cookie value; the cookie itself is never stored
    user_id       bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    user_agent    text
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE setup_links (
    id          bigserial PRIMARY KEY,
    token_hash  text NOT NULL UNIQUE,         -- only the hash is stored, so a database leak leaks no working links
    purpose     text NOT NULL CHECK (purpose IN ('invite', 'password')),  -- invite: a new person; password: set or reset for an existing user
    user_id     bigint REFERENCES users (id) ON DELETE CASCADE,
    created_by  bigint REFERENCES users (id) ON DELETE SET NULL,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    revoked_at  timestamptz
);
CREATE INDEX setup_links_user_idx ON setup_links (user_id);