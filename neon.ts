import { defineConfig } from "@neon/config/v1";

export default defineConfig({
  // Declare your Neon services here
  auth: false,
  functions: {
    // SIN central aggregator (src/sin/index.ts). Slug rule: ^[a-z0-9]{1,20}$.
    // Two keys, both sent as the X-SIN-Key header — the VALUE is the role:
    // SIN_INGEST_KEY is the node key every user node holds (POST /v1/report
    // only); SIN_ADMIN_KEY is held ONLY by the official MetaSync (it gates
    // POST /v1/aggregate and POST /v1/playlists, the canonical playlist
    // publish). Add both to the env file `neon deploy --env <file>` reads
    // (.env if it exists, otherwise .env.local) before deploying — a missing
    // value makes defineConfig throw, which is intended: never deploy the
    // write routes without their secrets.
    // API_KEY_PEPPER is the portal-key pepper (PORTAL-DESIGN.md §2.2/§3):
    // SIN hashes a presented per-user key with it and looks the hash up in
    // portal_keys. It MUST be the same value the portal app hashes with
    // when issuing keys — changing it invalidates every issued key, which
    // is why it is required here rather than optional.
    // SIN_TOKEN_PRIVATE_KEY is the pool's Ed25519 token-signing key
    // (§3A.4): the exchange signs short-lived access tokens with it and
    // GET /v1/auth/public-key publishes the derived public key. Accepted
    // formats: base64url of the raw 32-byte private seed (the JWK `d`
    // value node:crypto produces), base64url PKCS#8 DER, a JWK JSON
    // string, or PEM. Generate one with node:crypto generateKeyPairSync
    // ("ed25519") and export the private key's JWK. If it is somehow
    // absent at runtime the exchange fails closed (503
    // token_not_configured) rather than issuing unsigned tokens.
    sin: {
      name: "SIN aggregator",
      source: "src/sin/index.ts",
      env: {
        SIN_INGEST_KEY: process.env.SIN_INGEST_KEY!,
        SIN_ADMIN_KEY: process.env.SIN_ADMIN_KEY!,
        API_KEY_PEPPER: process.env.API_KEY_PEPPER!,
        SIN_TOKEN_PRIVATE_KEY: process.env.SIN_TOKEN_PRIVATE_KEY!,
      },
    },
    // MetaSync metadata service — SCAFFOLD ONLY (src/metasync/index.ts).
    // Health + a 501 placeholder route; no env, no DB access yet.
    metasync: {
      name: "MetaSync metadata (scaffold)",
      source: "src/metasync/index.ts",
    },
    // The original `hello` function from `neon init` was removed: its job
    // (proving the Neon Functions wiring) is now covered by the /health
    // routes on both functions above.
  },
  // Branch policy: per-branch tuning
  branch: (branch) => {
    if (branch.isDefault) {
      // Default branch: no overrides, uses project defaults
      return {};
    }
    if (!branch.exists) {
      // New non-default branches: auto-expire
      // Run `neon checkout <name>` to create a new branch with these settings
      return { ttl: "7d" };
    }
    // Existing branch: no changes
    return {};
  },
});
