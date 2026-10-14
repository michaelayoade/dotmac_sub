import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'expense_models.dart';

class ExpenseDescriptionField extends StatelessWidget {
  const ExpenseDescriptionField({super.key, required this.controller});

  final TextEditingController controller;

  @override
  Widget build(BuildContext context) =>
      ValueListenableBuilder<TextEditingValue>(
        valueListenable: controller,
        builder: (context, value, _) {
          final length = expenseDescriptionLength(value.text);
          return TextField(
            key: const Key('expense-description'),
            controller: controller,
            inputFormatters: [const _ExpenseDescriptionLengthFormatter()],
            decoration: InputDecoration(
              labelText: 'Description',
              helperText: 'Maximum $expenseDescriptionMaxLength characters.',
              counterText: '$length / $expenseDescriptionMaxLength',
              semanticCounterText:
                  '$length of $expenseDescriptionMaxLength characters',
            ),
          );
        },
      );
}

class _ExpenseDescriptionLengthFormatter extends TextInputFormatter {
  const _ExpenseDescriptionLengthFormatter();

  @override
  TextEditingValue formatEditUpdate(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    if (expenseDescriptionLength(newValue.text) <=
        expenseDescriptionMaxLength) {
      return newValue;
    }
    if (expenseDescriptionLength(oldValue.text) ==
            expenseDescriptionMaxLength &&
        oldValue.selection.isCollapsed) {
      return oldValue;
    }

    // Keep whole graphemes (including joined emoji) within the API's code-point
    // budget. Flutter's built-in maxLength counts graphemes instead.
    var length = 0;
    var graphemes = 0;
    for (final character in newValue.text.characters) {
      final nextLength = length + expenseDescriptionLength(character);
      if (nextLength > expenseDescriptionMaxLength) break;
      length = nextLength;
      graphemes += 1;
    }
    final truncated = newValue.text.characters.take(graphemes).toString();
    return newValue.copyWith(
      text: truncated,
      selection: newValue.selection.copyWith(
        baseOffset: math.min(newValue.selection.baseOffset, truncated.length),
        extentOffset: math.min(
          newValue.selection.extentOffset,
          truncated.length,
        ),
      ),
      composing:
          newValue.composing.isValid &&
              newValue.composing.start < truncated.length
          ? TextRange(
              start: newValue.composing.start,
              end: math.min(newValue.composing.end, truncated.length),
            )
          : TextRange.empty,
    );
  }
}
