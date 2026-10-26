const PREFIX = "rf-result-gzip-v1:";
const MAX_STORED_BYTES = 1_900_000;
const MAX_DECODED_BYTES = 12 * 1024 * 1024;

export async function encodeRecommendation(value: unknown) {
  const json = JSON.stringify(value);
  if (json === undefined) throw new Error("Resultado do planejamento inválido.");
  const input = new TextEncoder().encode(json);
  if (input.length > MAX_DECODED_BYTES) throw new Error("O resultado excede o limite de armazenamento do planejamento.");
  const compressed = new Uint8Array(await new Response(new Blob([input]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer());
  let binary = "";
  for (let index = 0; index < compressed.length; index += 32768) {
    binary += String.fromCharCode(...compressed.subarray(index, index + 32768));
  }
  const encoded = PREFIX + btoa(binary);
  if (encoded.length > MAX_STORED_BYTES) throw new Error("O resultado excede o limite de armazenamento do planejamento.");
  return encoded;
}

export async function decodeRecommendation(value: string): Promise<unknown> {
  if (!value.startsWith(PREFIX)) {
    if (new TextEncoder().encode(value).length > MAX_DECODED_BYTES) throw new Error("Resultado salvo excede o limite de leitura.");
    return JSON.parse(value);
  }
  if (value.length > MAX_STORED_BYTES) throw new Error("Resultado salvo excede o limite de leitura.");
  const binary = atob(value.slice(PREFIX.length));
  const input = Uint8Array.from(binary, character => character.charCodeAt(0));
  const stream = new Blob([input]).stream().pipeThrough(new DecompressionStream("gzip"));
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value: chunk, done } = await reader.read();
      if (done) break;
      size += chunk.length;
      if (size > MAX_DECODED_BYTES) {
        await reader.cancel();
        throw new Error("Resultado salvo excede o limite de leitura.");
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }
  const decoded = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    decoded.set(chunk, offset);
    offset += chunk.length;
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(decoded));
}
