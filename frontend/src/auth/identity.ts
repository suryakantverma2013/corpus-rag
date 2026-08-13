/**
 * `MeResponse` → the FR-SBR-06 sidebar row.
 *
 * The wire carries `email` (always) and `display_name` (nullable — FR-USR-03 makes it optional
 * at creation, and T-110 records that Keycloak will happily hold a user with none). The row
 * needs a name and a pair of initials regardless, so both fall back to the email rather than
 * rendering an empty avatar beside an empty name.
 */
import type { Me } from '../api';

/** What FR-SBR-06 renders beside the version tag. */
export function displayName(user: Me): string {
  const name = user.display_name?.trim();
  return name !== undefined && name.length > 0 ? name : localPart(user.email);
}

/**
 * Up to two initials for the 26px avatar.
 *
 * From the display name's first two words when there is one ("Maya Jensen" → "MJ"), otherwise
 * from the email's local part, splitting on the separators people actually use in addresses so
 * `jane.doe@…` gives "JD" rather than "JA". A single word yields a single letter — two letters
 * of one word ("MA" for "maya") reads as a different person's initials.
 */
export function initials(user: Me): string {
  const words = displayName(user)
    .split(/[\s._-]+/u)
    .filter((word) => word.length > 0);
  return words
    .slice(0, 2)
    .map((word) => [...word][0]?.toUpperCase() ?? '')
    .join('');
}

function localPart(email: string): string {
  const at = email.indexOf('@');
  return at === -1 ? email : email.slice(0, at);
}
