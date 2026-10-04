export function parseBooleanConfigValue(value: unknown): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0

  const normalized = String(value ?? '')
    .trim()
    .toLowerCase()

  return ['1', 'true', 'yes', 'on'].includes(normalized)
}
