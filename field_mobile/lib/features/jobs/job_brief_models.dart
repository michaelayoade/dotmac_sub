/// Read-only wire contract for the assignment-scoped field job brief.
enum BriefAudienceScope { current, issued }

class BriefRevision {
  const BriefRevision({
    required this.id,
    required this.sequence,
    required this.knowledge,
    this.subscriptionCount,
  });
  final String id;
  final int sequence;
  final String knowledge;
  final int? subscriptionCount;
  factory BriefRevision.fromJson(Map<String, dynamic> json) => BriefRevision(
    id: json['id'] as String,
    sequence: json['sequence'] as int,
    knowledge: json['knowledge'] as String,
    subscriptionCount: json['subscription_count'] as int?,
  );
}

class BriefAudience {
  const BriefAudience({
    required this.linkId,
    required this.issued,
    this.current,
    this.nextAfter,
  });
  final String linkId;
  final BriefRevision issued;
  final BriefRevision? current;
  final String? nextAfter;
  factory BriefAudience.fromJson(Map<String, dynamic> json) => BriefAudience(
    linkId: json['link_id'] as String,
    issued: BriefRevision.fromJson(
      (json['issued'] as Map).cast<String, dynamic>(),
    ),
    current: json['current'] == null
        ? null
        : BriefRevision.fromJson(
            (json['current'] as Map).cast<String, dynamic>(),
          ),
    nextAfter: json['next_after_subscription_id'] as String?,
  );
}

class BriefCustomer {
  const BriefCustomer({
    required this.subscriptionId,
    this.name,
    this.address,
    required this.detailsAvailable,
  });
  final String subscriptionId;
  final String? name;
  final String? address;
  final bool detailsAvailable;
  factory BriefCustomer.fromJson(Map<String, dynamic> json) => BriefCustomer(
    subscriptionId: json['subscription_id'] as String,
    name: json['name'] as String?,
    address: json['service_address'] as String?,
    detailsAvailable: json['details_available'] as bool,
  );
}

class BriefDirection {
  const BriefDirection({
    required this.label,
    required this.provenance,
    this.latitude,
    this.longitude,
    this.address,
  });
  final String label;
  final String provenance;
  final double? latitude;
  final double? longitude;
  final String? address;
  factory BriefDirection.fromJson(Map<String, dynamic> json) => BriefDirection(
    label: json['label'] as String,
    provenance: json['coordinate_provenance'] as String,
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    address: json['address_text'] as String?,
  );
}

class BriefTask {
  const BriefTask({
    required this.title,
    required this.status,
    this.instructions,
  });
  final String title;
  final String status;
  final String? instructions;
  factory BriefTask.fromJson(Map<String, dynamic> json) => BriefTask(
    title: json['title'] as String,
    status: json['status'] as String,
    instructions: json['instructions'] as String?,
  );
}

class BriefSignoff {
  const BriefSignoff({
    required this.evidenceRequired,
    required this.minimumPhotos,
    required this.customerSignoffRequired,
    required this.asBuiltRequired,
  });
  final bool evidenceRequired;
  final int minimumPhotos;
  final bool customerSignoffRequired;
  final bool asBuiltRequired;
  factory BriefSignoff.fromJson(Map<String, dynamic> json) => BriefSignoff(
    evidenceRequired: json['evidence_required'] as bool,
    minimumPhotos: json['minimum_photo_count'] as int,
    customerSignoffRequired: json['customer_signoff_required'] as bool,
    asBuiltRequired: json['requires_as_built_evidence'] as bool,
  );
}

class BriefPlanningGuidance {
  const BriefPlanningGuidance({
    required this.purpose,
    required this.technology,
    required this.evidence,
  });
  final String purpose;
  final String technology;
  final List<String> evidence;
  factory BriefPlanningGuidance.fromJson(Map<String, dynamic> json) {
    if (json['use'] != 'recommendation_only') {
      throw const FormatException('Unsupported planning guidance use');
    }
    final classification = (json['classification'] as Map)
        .cast<String, dynamic>();
    return BriefPlanningGuidance(
      purpose: classification['purpose'] as String,
      technology: classification['access_technology'] as String,
      evidence: (json['requirements'] as List)
          .cast<Map>()
          .map((item) => item['evidence'] as String)
          .toList(growable: false),
    );
  }
}

class JobBrief {
  const JobBrief({
    required this.publicId,
    this.issue,
    this.instructions,
    this.accessNotes,
    this.audience,
    this.affectedSubscriptionCount,
    required this.customers,
    required this.tasks,
    required this.directions,
    required this.signoff,
    this.planningGuidance,
    this.planningGuidanceStatus,
  });
  final String publicId;
  final String? issue;
  final String? instructions;
  final String? accessNotes;
  final BriefAudience? audience;
  final int? affectedSubscriptionCount;
  final List<BriefCustomer> customers;
  final List<BriefTask> tasks;
  final List<BriefDirection> directions;
  final BriefSignoff signoff;
  final BriefPlanningGuidance? planningGuidance;
  final String? planningGuidanceStatus;
  factory JobBrief.fromJson(Map<String, dynamic> json) => JobBrief(
    planningGuidance: json['template_recommendation'] == null
        ? null
        : BriefPlanningGuidance.fromJson(
            (json['template_recommendation'] as Map).cast<String, dynamic>(),
          ),
    planningGuidanceStatus: json['template_recommendation_status'] as String?,
    publicId: json['public_id'] as String,
    issue: json['issue'] as String?,
    instructions: json['instructions'] as String?,
    accessNotes: json['access_notes'] as String?,
    audience: json['outage_audience'] == null
        ? null
        : BriefAudience.fromJson(
            (json['outage_audience'] as Map).cast<String, dynamic>(),
          ),
    affectedSubscriptionCount: json['affected_subscription_count'] as int?,
    customers: (json['affected_customers'] as List)
        .cast<Map>()
        .map((item) => BriefCustomer.fromJson(item.cast<String, dynamic>()))
        .toList(growable: false),
    tasks: (json['tasks'] as List)
        .cast<Map>()
        .map((item) => BriefTask.fromJson(item.cast<String, dynamic>()))
        .toList(growable: false),
    directions: (json['directions'] as List)
        .cast<Map>()
        .map((item) => BriefDirection.fromJson(item.cast<String, dynamic>()))
        .toList(growable: false),
    signoff: BriefSignoff.fromJson(
      (json['signoff'] as Map).cast<String, dynamic>(),
    ),
  );
}
