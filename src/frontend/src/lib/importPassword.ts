const ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const PASSWORD_LENGTH = 12;

export function generateImportPassword(): string {
  // Reject the partial final range so each character has equal probability.
  const limit = 256 - (256 % ALPHABET.length);
  const bytes = new Uint8Array(PASSWORD_LENGTH);
  let password = "";
  while (password.length < PASSWORD_LENGTH) {
    crypto.getRandomValues(bytes);
    for (const byte of bytes) {
      if (byte < limit && password.length < PASSWORD_LENGTH) {
        password += ALPHABET[byte % ALPHABET.length];
      }
    }
  }
  return password;
}
