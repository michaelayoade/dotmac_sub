import 'package:flutter/material.dart';

/// DotMac "Industrial Modern", outdoors edition: high contrast, calm status
/// colours, glove-grade touch targets, Outfit + Plus Jakarta Sans. Mirrors the
/// web design-system tokens so field and CRM read as one family.
abstract final class AppColors {
  static const primary = Color(0xFF06B6D4); // teal
  static const primaryDeep = Color(0xFF0E7490); // teal-700 (gradients/pressed)
  static const accent = Color(0xFFF97316); // warm orange

  // Cool, teal-biased neutrals — never flat grey.
  static const ink = Color(0xFF0F172A);
  static const inkSoft = Color(0xFF475569);
  static const inkFaint = Color(0xFF94A3B8);
  static const groundLight = Color(0xFFE9EEF4);
  static const surfaceLight = Color(0xFFFFFFFF);
  static const lineLight = Color(0xFFE3E9F0);
  static const muted = inkSoft;
  static const panel = surfaceLight;

  static const inkDark = Color(0xFFEEF4FB);
  static const inkSoftDark = Color(0xFFA7B4C6);
  static const inkFaintDark = Color(0xFF6B7A90);
  static const groundDark = Color(0xFF080D16);
  static const surfaceDark = Color(0xFF101A2B);
  static const lineDark = Color(0xFF1E2B40);
  static const green = Color(0xFF10B981);
  static const greenSoft = Color(0xFFD1FAE5);
  static const tealSoft = Color(0xFFCFFAFE);
  static const danger = Color(0xFFEF4444);

  // Work-type colours (left card bars, chips) — same mapping as dispatch web.
  static const workTypeColors = <String, Color>{
    'install': Color(0xFFF59E0B), // amber
    'repair': Color(0xFFF43F5E), // rose
    'survey': Color(0xFF8B5CF6), // violet
    'maintenance': Color(0xFF06B6D4), // cyan
    'disconnect': Color(0xFF64748B), // slate
    'other': Color(0xFF64748B),
  };

  // One status ramp shared with dispatch.
  static const statusColors = <String, Color>{
    'scheduled': Color(0xFF64748B), // slate
    'dispatched': Color(0xFF6366F1), // indigo
    'accepted': Color(0xFF06B6D4), // cyan
    'en_route': Color(0xFFF97316), // orange
    'in_progress': Color(0xFF3B82F6), // blue
    'paused': Color(0xFFF59E0B), // amber
    'completed': Color(0xFF10B981), // emerald
    'hold': Color(0xFFF59E0B), // amber
    'canceled': Color(0xFF94A3B8),
  };

  static const _statusLabels = <String, String>{
    'scheduled': 'Scheduled',
    'dispatched': 'Assigned',
    'accepted': 'Accepted',
    'en_route': 'En route',
    'in_progress': 'Active',
    'paused': 'Paused',
    'completed': 'Completed',
    'hold': 'On hold',
    'canceled': 'Canceled',
  };

  static Color workType(String type) =>
      workTypeColors[type] ?? workTypeColors['other']!;
  static Color status(String status) =>
      statusColors[status] ?? statusColors['scheduled']!;
  static String statusLabel(String status) =>
      _statusLabels[status] ??
      status
          .replaceAll('_', ' ')
          .replaceFirstMapped(RegExp(r'^\w'), (m) => m[0]!.toUpperCase());
  static bool dark(BuildContext context) =>
      Theme.of(context).brightness == Brightness.dark;
  static Color surface(BuildContext context) =>
      dark(context) ? surfaceDark : surfaceLight;
  static Color text(BuildContext context) => dark(context) ? inkDark : ink;
  static Color subdued(BuildContext context) =>
      dark(context) ? inkSoftDark : inkSoft;
  static Color border(BuildContext context) =>
      dark(context) ? lineDark : lineLight;
  static Color softGreen(BuildContext context) =>
      dark(context) ? const Color(0xFF0F2E25) : greenSoft;
  static Color softTeal(BuildContext context) =>
      dark(context) ? const Color(0xFF10333D) : tealSoft;
}

/// 4-based spacing scale. Use these instead of magic numbers.
abstract final class AppSpace {
  static const xs = 4.0;
  static const sm = 8.0;
  static const md = 12.0;
  static const lg = 16.0;
  static const xl = 20.0;
  static const xxl = 24.0;
}

abstract final class AppRadii {
  static const chip = 8.0;
  static const control = 12.0; // buttons, inputs
  static const tile = 16.0; // stat tiles, small cards
  static const card = 16.0; // legacy (existing screens)
  static const bigCard = 20.0; // job cards, feature cards
  static const feature = 24.0;
  static const pill = 999.0;
}

abstract final class AppSizes {
  /// Glove-grade primary actions.
  static const primaryTouchTarget = 56.0;
  static const touchTarget = 48.0;
}

TextTheme _textTheme(Color ink, Color inkSoft) {
  const disp = 'Outfit';
  const body = 'PlusJakartaSans';
  return TextTheme(
    displaySmall: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w800,
      fontSize: 30,
      height: 1.05,
      letterSpacing: -0.5,
      color: ink,
    ),
    headlineMedium: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w800,
      fontSize: 25,
      height: 1.1,
      letterSpacing: -0.4,
      color: ink,
    ),
    headlineSmall: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w700,
      fontSize: 22,
      height: 1.15,
      letterSpacing: -0.3,
      color: ink,
    ),
    titleLarge: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w700,
      fontSize: 18,
      letterSpacing: -0.2,
      color: ink,
    ),
    titleMedium: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w700,
      fontSize: 16,
      height: 1.25,
      color: ink,
    ),
    titleSmall: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w600,
      fontSize: 14,
      color: ink,
    ),
    bodyLarge: TextStyle(
      fontFamily: body,
      fontSize: 15.5,
      height: 1.45,
      color: ink,
    ),
    bodyMedium: TextStyle(
      fontFamily: body,
      fontSize: 14,
      height: 1.45,
      color: inkSoft,
    ),
    bodySmall: TextStyle(
      fontFamily: body,
      fontSize: 12.5,
      height: 1.4,
      color: inkSoft,
    ),
    labelLarge: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w700,
      fontSize: 14.5,
      color: ink,
    ),
    labelMedium: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w600,
      fontSize: 12,
      color: inkSoft,
    ),
    labelSmall: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w700,
      fontSize: 11,
      letterSpacing: 0.5,
      color: inkSoft,
    ),
  );
}

ThemeData _base(Brightness brightness) {
  final isDark = brightness == Brightness.dark;
  final scheme = ColorScheme.fromSeed(
    seedColor: AppColors.primary,
    brightness: brightness,
    primary: AppColors.primary,
    secondary: AppColors.accent,
    surface: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
  );
  final ink = isDark ? AppColors.inkDark : AppColors.ink;
  final inkSoft = isDark ? AppColors.inkSoftDark : AppColors.inkSoft;
  final line = isDark ? AppColors.lineDark : AppColors.lineLight;

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    fontFamily: 'PlusJakartaSans',
    textTheme: _textTheme(ink, inkSoft),
    scaffoldBackgroundColor: isDark
        ? AppColors.groundDark
        : AppColors.groundLight,
    dividerColor: line,
    cardTheme: CardThemeData(
      elevation: 0,
      color: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.bigCard),
        side: BorderSide(color: line),
      ),
      margin: EdgeInsets.zero,
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size.fromHeight(AppSizes.primaryTouchTarget),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
        textStyle: const TextStyle(
          fontFamily: 'PlusJakartaSans',
          fontSize: 15.5,
          fontWeight: FontWeight.w700,
        ),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size.fromHeight(AppSizes.touchTarget),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppRadii.control),
        borderSide: BorderSide(color: line),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppRadii.control),
        borderSide: BorderSide(color: line),
      ),
      contentPadding: const EdgeInsets.symmetric(
        horizontal: AppSpace.lg,
        vertical: AppSpace.lg,
      ),
    ),
    chipTheme: ChipThemeData(
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.pill),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      height: 72,
      elevation: 0,
      backgroundColor: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
    ),
  );
}

final lightTheme = _base(Brightness.light);
final darkTheme = _base(Brightness.dark);
