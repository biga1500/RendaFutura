export const MAX_REQUEST_BODY_BYTES = 16 * 1024;

export class RequestBodyTooLargeError extends Error {
  constructor() {
    super("Request body exceeds 16 KiB.");
    this.name = "RequestBodyTooLargeError";
  }
}

export async function readRequestBody(request: Request): Promise<string> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const bytes = new Uint8Array(MAX_REQUEST_BODY_BYTES);
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value.byteLength > MAX_REQUEST_BODY_BYTES - size) {
        // A stalled transport cancellation must not delay the 413 response.
        void reader.cancel().catch(() => {});
        throw new RequestBodyTooLargeError();
      }
      bytes.set(value, size);
      size += value.byteLength;
    }
    return new TextDecoder().decode(bytes.subarray(0, size));
  } finally {
    reader.releaseLock();
  }
}
