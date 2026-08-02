# fix(maas-api): use ExternalModel name as model ID in GET /v1/models [RHOAIENG-63297]

When a MaaSModelRef references an ExternalModel, the GET /v1/models
catalog endpoint was returning the MaaSModelRef resource name as the
model ID. Inference endpoints (/v1/chat/completions) require the
ExternalModel CR name in the request body, so clients copying the ID
from the catalog would get mismatched or failed requests.

The root cause is that ExternalModel-backed refs skip the backend
/v1/models probe (since external providers need the provider API key,
not the user's MaaS token), so unlike LLMInferenceService models, their
ID was never corrected by discoveredToModels.

The fix reads spec.modelRef.name from the MaaSModelRef and uses it as
the model ID when spec.modelRef.kind is ExternalModel. OwnedBy still
uses the MaaSModelRef name for dashboard display and deduplication.

Closes [RHOAIENG-63297](https://redhat.atlassian.net/browse/RHOAIENG-63297)

Co-Authored-By: Claude <noreply@anthropic.com>
Signed-off-by: Jamie Land <jland@redhat.com>

<!-- This is an auto-generated comment: release notes by coderabbit.ai -->

## Summary by CodeRabbit

## Release Notes

* **Bug Fixes**
  * External models in API responses now use their reference identifiers instead of resource names, ensuring consistency with inference endpoint expectations.

* **Tests**
  * Added comprehensive test coverage for external model identifier resolution, validating correct API response structure and values.

<!-- end of auto-generated comment: release notes by coderabbit.ai -->

## Files involved
- `maas-api/internal/handlers/models_test.go`
- `maas-api/internal/models/maasmodelref.go`
