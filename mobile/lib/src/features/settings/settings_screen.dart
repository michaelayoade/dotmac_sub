import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../config/env.dart';
import '../../providers/theme_controller.dart';

/// App settings: appearance (theme) and an About section. Account-level settings
/// (profile, password, sessions, payment methods, biometric lock) live under
/// Profile.
class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final mode = ref.watch(themeModeProvider);
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        children: [
          const _Header('Appearance'),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: SegmentedButton<ThemeMode>(
              segments: const [
                ButtonSegment(
                  value: ThemeMode.system,
                  label: Text('System'),
                  icon: Icon(Icons.brightness_auto_outlined),
                ),
                ButtonSegment(
                  value: ThemeMode.light,
                  label: Text('Light'),
                  icon: Icon(Icons.light_mode_outlined),
                ),
                ButtonSegment(
                  value: ThemeMode.dark,
                  label: Text('Dark'),
                  icon: Icon(Icons.dark_mode_outlined),
                ),
              ],
              selected: {mode},
              onSelectionChanged: (s) =>
                  ref.read(themeModeProvider.notifier).set(s.first),
            ),
          ),
          const SizedBox(height: 12),
          const Divider(),
          const _Header('About'),
          const ListTile(
            leading: Icon(Icons.info_outline),
            title: Text(Brand.name),
            subtitle: Text('Version ${Brand.version}'),
          ),
          if (Brand.legalName.isNotEmpty)
            const ListTile(
              leading: Icon(Icons.business_outlined),
              title: Text('Provided by'),
              subtitle: Text(Brand.legalName),
            ),
          if (Brand.supportEmail.isNotEmpty)
            const ListTile(
              leading: Icon(Icons.support_agent_outlined),
              title: Text('Support'),
              subtitle: Text(Brand.supportEmail),
            ),
          const SizedBox(height: 16),
          Center(
            child: Text(
              Brand.tagline,
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
      child: Text(
        text.toUpperCase(),
        style: theme.textTheme.labelMedium?.copyWith(
          color: theme.colorScheme.primary,
        ),
      ),
    );
  }
}
