declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    BUCKET?: R2Bucket;
    RECOMMENDER_URL?: string;
    RECOMMENDER_TOKEN?: string;
  }
}
