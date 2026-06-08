// JSON parsing helpers shared by the data models.
//
// The backend serializes `Decimal` money fields as **strings** (e.g.
// `"4999.00"`) and other numbers as JSON numbers, so a plain `as num` cast
// throws on the string form. These helpers accept either.

// Parse a possibly-Decimal-string number; null when absent/unparseable.
double? asDoubleOrNull(dynamic v) {
  if (v == null) return null;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString());
}

/// Like [asDoubleOrNull] but defaults to `0` when absent/unparseable.
double asDouble(dynamic v) => asDoubleOrNull(v) ?? 0;
