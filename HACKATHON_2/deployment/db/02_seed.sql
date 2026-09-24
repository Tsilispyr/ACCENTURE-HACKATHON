-- Two users with DIFFERENT SCOPES. This pair is the 30-second security demo:
-- alice can read payments incidents, bob cannot, and no prompt changes that.
--
-- Hashes are bcrypt of 'demo1234'. Local demo credentials only.
INSERT INTO app_user (id, email, password_hash, role, scope) VALUES
    ('u-alice', 'alice@example.com',
     '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj4J/HS.hK8K', 'engineer', 'payments'),
    ('u-bob',   'bob@example.com',
     '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj4J/HS.hK8K', 'engineer', 'platform'),
    ('u-admin', 'admin@example.com',
     '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewdBPj4J/HS.hK8K', 'admin', 'public')
ON CONFLICT (id) DO NOTHING;
