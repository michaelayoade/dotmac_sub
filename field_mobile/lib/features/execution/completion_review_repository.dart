import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_client.dart';
import '../auth/auth_state.dart';
import 'completion_review_models.dart';

class CompletionReviewRefusal implements Exception {
  const CompletionReviewRefusal({required this.code, required this.message});
  final String code;
  final String message;
}

class CompletionReviewRepository {
  const CompletionReviewRepository(this.api);
  final ApiClient api;
  String _path(String jobId) =>
      '/api/v1/field/jobs/${Uri.encodeComponent(jobId)}/completion-review';

  Future<CompletionReviewPreview> preview(String jobId) async {
    try {
      final response = await api.dio.get<Map<String, dynamic>>(_path(jobId));
      return CompletionReviewPreview.fromJson(response.data!);
    } on DioException catch (error) {
      throw _refusal(error);
    }
  }

  Future<CompletionEvidenceReceipt> submit(
    String jobId,
    CompletionEvidenceIntent intent,
  ) async {
    try {
      final response = await api.dio.post<Map<String, dynamic>>(
        '${_path(jobId)}/submissions',
        data: intent.toJson(),
      );
      return CompletionEvidenceReceipt.fromJson(response.data!);
    } on DioException catch (error) {
      throw _refusal(error);
    }
  }

  Exception _refusal(DioException error) {
    final status = error.response?.statusCode;
    if (status != null && status >= 400 && status < 500) {
      final data = error.response?.data;
      final detail = data is Map ? data['detail'] : null;
      return CompletionReviewRefusal(
        code: detail is Map && detail['code'] is String
            ? detail['code'] as String
            : 'completion_refused',
        message: detail is Map && detail['message'] is String
            ? detail['message'] as String
            : detail is String
            ? detail
            : 'Completion evidence was refused. Reload the issued criteria.',
      );
    }
    return error;
  }
}

final completionReviewRepositoryProvider = Provider<CompletionReviewRepository>(
  (ref) => CompletionReviewRepository(ref.watch(apiClientProvider)),
);
final completionReviewPreviewProvider = FutureProvider.autoDispose
    .family<CompletionReviewPreview, String>(
      (ref, jobId) =>
          ref.watch(completionReviewRepositoryProvider).preview(jobId),
    );
