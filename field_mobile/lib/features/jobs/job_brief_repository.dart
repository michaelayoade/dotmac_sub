import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_state.dart';
import 'job_brief_models.dart';

typedef JobBriefRequest = ({String jobId, BriefAudienceScope scope});

class JobBriefRepository {
  const JobBriefRepository(this.ref);
  final Ref ref;

  Future<JobBrief> fetch(
    JobBriefRequest request, {
    String? linkId,
    String? revisionId,
    String? after,
  }) async {
    final response = await ref
        .read(apiClientProvider)
        .dio
        .get(
          '/api/v1/field/jobs/${Uri.encodeComponent(request.jobId)}/brief',
          queryParameters: {
            'audience_scope': request.scope.name,
            'audience_link_id': ?linkId,
            'expected_revision_id': ?revisionId,
            'after_subscription_id': ?after,
            'limit': 25,
          },
        );
    return JobBrief.fromJson((response.data as Map).cast<String, dynamic>());
  }
}

final jobBriefRepositoryProvider = Provider<JobBriefRepository>(
  JobBriefRepository.new,
);
final jobBriefProvider = FutureProvider.autoDispose
    .family<JobBrief, JobBriefRequest>(
      (ref, request) => ref.watch(jobBriefRepositoryProvider).fetch(request),
    );
