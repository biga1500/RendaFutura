import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const users = sqliteTable("users", {
  id: text("id").primaryKey(),
  alias: text("alias").notNull(),
  email: text("email").notNull().unique(),
  passwordHash: text("password_hash").notNull(),
  passwordSalt: text("password_salt").notNull(),
  consentAt: text("consent_at").notNull(),
  consentVersion: text("consent_version").notNull(),
  createdAt: text("created_at").notNull(),
});
export const sessions = sqliteTable("sessions", {
  tokenHash: text("token_hash").primaryKey(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete:"cascade" }),
  expiresAt: integer("expires_at").notNull(),
});
export const profiles = sqliteTable("profiles", {
  userId: text("user_id").primaryKey().references(() => users.id, { onDelete:"cascade" }),
  answers: text("answers").notNull(),
  result: text("result"),
  status: text("status").notNull(),
  step: integer("step").notNull(),
  consentAt: text("consent_at").notNull(),
  consentVersion: text("consent_version").notNull(),
  updatedAt: text("updated_at").notNull(),
});
export const attempts = sqliteTable("attempts", {
  key: text("key").primaryKey(),
  count: integer("count").notNull(),
  expiresAt: integer("expires_at").notNull(),
});
