CREATE TABLE `recommendations` (
	`user_id` text PRIMARY KEY NOT NULL,
	`profile_updated_at` text NOT NULL,
	`settings` text NOT NULL,
	`result` text NOT NULL,
	`consent_at` text NOT NULL,
	`consent_version` text NOT NULL,
	`updated_at` text NOT NULL,
	FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON UPDATE no action ON DELETE cascade
);
