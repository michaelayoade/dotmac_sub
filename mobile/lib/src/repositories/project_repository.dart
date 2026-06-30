import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/project.dart';

/// Wraps the self-scoped installation-tracker endpoint (app/api/me.py,
/// /me/projects). Reads come from the sub's local project mirror.
class ProjectRepository {
  ProjectRepository(this.dio);

  final Dio dio;

  /// GET /me/projects — projects with stage timeline + progress %.
  Future<ProjectsSummary> summary() async {
    final data = await guard(() => dio.get('/me/projects'));
    return ProjectsSummary.fromJson(data as Map<String, dynamic>);
  }
}
