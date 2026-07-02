import 'package:intl/intl.dart';

/// Formatting helpers shared across screens.
class Fmt {
  const Fmt._();

  static final _date = DateFormat('d MMM yyyy');
  static final _dateTime = DateFormat('d MMM yyyy, HH:mm');

  static String date(DateTime? d) => d == null ? '—' : _date.format(d);

  static String dateTime(DateTime? d) => d == null ? '—' : _dateTime.format(d);

  /// Currency using the ISO code as the symbol (e.g. "NGN 1,250.00"), which
  /// avoids guessing a locale-specific glyph for arbitrary backend currencies.
  static String money(num amount, String currencyCode) {
    final f = NumberFormat.currency(symbol: '$currencyCode ', decimalDigits: 2);
    return f.format(amount);
  }

  /// Compact currency for tight at-a-glance spots (e.g. dashboard stat cards),
  /// where a full figure like "NGN 1,732,000.00" overflows a narrow column.
  /// Abbreviates large amounts ("NGN 1.7M", "NGN 250K") and falls back to the
  /// full [money] format below 10,000 where precision still fits. The exact
  /// figure is always available on the screen the card links to.
  static String moneyCompact(num amount, String currencyCode) {
    final abs = amount.abs();
    if (abs < 10000) return money(amount, currencyCode);
    final sign = amount < 0 ? '-' : '';
    return '$currencyCode $sign${NumberFormat.compact().format(abs)}';
  }

  /// Human-readable byte size from an octet count.
  static String bytes(int octets) {
    if (octets <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    var value = octets.toDouble();
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit++;
    }
    final digits = value >= 100 || unit == 0 ? 0 : 1;
    return '${value.toStringAsFixed(digits)} ${units[unit]}';
  }

  /// Link rate: decimal units (1 Mbps = 1,000,000 bps), unlike [bytes].
  static String bps(num? value) {
    final v = (value ?? 0).toDouble();
    if (v <= 0) return '0 bps';
    const units = ['bps', 'Kbps', 'Mbps', 'Gbps'];
    var amount = v;
    var unit = 0;
    while (amount >= 1000 && unit < units.length - 1) {
      amount /= 1000;
      unit++;
    }
    final digits = amount >= 100 || unit == 0 ? 0 : 1;
    return '${amount.toStringAsFixed(digits)} ${units[unit]}';
  }

  static String gb(num value) =>
      '${value.toStringAsFixed(value >= 100 ? 0 : 1)} GB';

  /// Coarse, single-unit duration for status text (e.g. "3h", "12m", "2d").
  static String compactDuration(Duration d) {
    if (d.inDays >= 1) return '${d.inDays}d';
    if (d.inHours >= 1) return '${d.inHours}h';
    if (d.inMinutes >= 1) return '${d.inMinutes}m';
    return 'just now';
  }

  /// How long ago [since] was, as a compact uptime label.
  static String uptime(DateTime since) {
    final d = DateTime.now().difference(since);
    return d.isNegative ? 'just now' : compactDuration(d);
  }
}
