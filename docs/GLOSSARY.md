# Glossary

Use **SCIM client** for this application and **SCIM service provider** for the remote system throughout documentation, code explanations, and UI text. “Local” means the client's SQLite state. “Remote” means the service provider's state. Sync is provisioning toward the service provider, even when it includes reads from that service provider.

## Roles and data

| Term | Meaning |
| --- | --- |
| SCIM | System for Cross-domain Identity Management. SCIM 2.0 defines identity resource schemas (RFC 7643) and an HTTP protocol (RFC 7644). |
| SCIM client | The component initiating SCIM requests. This application maintains local desired state and provisions it on explicit operator request. Its web server serves the operator UI, not a SCIM API. |
| SCIM service provider | The remote system implementing SCIM endpoints, storing remote resources, allocating resource IDs, and enforcing authentication and company boundaries. Not called the client or identity provider in these docs. |
| Identity provider | A system authenticating people, often through OpenID Connect or SAML. Not synonymous with a SCIM service provider. SCIM provisioning does not log users into applications. |
| Operator | The person using this client's local web UI. The provider sees the bearer credential, not this person's identity. |
| Resource | A SCIM object with a resource type, schema, and provider-assigned `id`. User and Group are resource types. |
| User | A SCIM resource representing an account on the service provider. A local user row describes the desired attributes for that account. It is not necessarily a globally unique human identity. |
| Group | A SCIM resource representing a collection of members, often an entitlement or role. This client treats group definitions as provider-owned. |
| Group reference / catalog reference | A local `groups` row describing a pre-existing provider Group by company and display name, with a cached provider ID after correlation. Inserting a reference does not create a remote Group. |
| Membership / assignment | A relationship between a User and Group. Local `memberships` rows represent desired assignments; a Group's SCIM `members` array contains references to provider resource IDs. This client supports user members, not nested groups. |
| Entitlement | An access right available for assignment. A provider Group may represent an entitlement, but the provider defines the actual authorization effect. |
| Company / tenant | An isolation boundary for accounts and entitlements. The client stores external company identifiers; the provider maps them to explicitly onboarded internal tenants. A local company does not automatically create a provider tenant. |
| `companyId` | Immutable external company identifier carried in the configured custom extensions on Users and Groups. A user and assigned group must belong to the same company. |
| Desired state | The local user fields, lifecycle state, and memberships the operator wants on the provider. Editing desired state does not itself make SCIM requests. |
| Remote state | Resource attributes currently returned by the service provider. Read for comparison, not imported into local desired state. |
| Managed resource | A local user or group reference with a correlated remote resource, or one awaiting provisioning/correlation. Only managed fields and managed user memberships are reconciled. |
| Remote-only | A remote resource without a matching local managed resource. It is not deleted or imported. In a managed Group, a remote-only membership is a membership whose user has no local managed provider-ID mapping; it is preserved. |

## Identifiers and schema

| Term | Meaning |
| --- | --- |
| Local ID | Stable primary key in SQLite. For users it is sent as SCIM `externalId`; for local groups it is only a local catalog key. |
| `id` / provider ID / remote ID | Stable opaque SCIM resource identifier assigned by the service provider. Cached in the client's `remote_id` column and used in resource URLs and member references. It is not a username. |
| `externalId` | Client-supplied correlation identifier. This client sends its immutable local user ID. The service provider must preserve it and support equality filtering on it. |
| `userName` | User's mutable account name. The client requires local uniqueness but does not use it as the stable correlation key. Local column name: `username`. |
| `displayName` | Human-readable Group name. Combined with company ID for initial group correlation. Existing groups are subsequently addressed by provider ID. |
| Correlation | Matching local and remote resources. Users match on external ID plus company; groups initially match on display name plus company. Ambiguous matches fail rather than guessing. |
| Mapping | Cached association between local ID and provider ID. Also used in the domain for the provider's separate external-company-to-internal-tenant association. |
| Schema | Definition of resource attributes, types, and mutability. A SCIM resource's `schemas` array lists applicable schema URNs. |
| URN | Uniform Resource Name. Used as a stable schema identifier, not a URL to fetch. |
| Extension | Additional attributes under a schema URN. This client uses configurable User and Group extensions containing `companyId`. |
| `active` | User lifecycle boolean. False means disabled; the resource still exists. Local storage uses SQLite integers 0 and 1. |
| `members` | Group attribute listing resource references, usually `{ "value": "provider-user-id" }`. The client patches this attribute rather than the read-only User `groups` attribute. |

## Requests and responses

| Term | Meaning |
| --- | --- |
| Base URL | Root of a provider's SCIM API, including any prefix such as `/scim/v2`. Resource paths are appended to it. |
| Endpoint | A resource collection or resource URL, for example `/Users`, `/Users/{id}`, `/Groups`, or `/Groups/{id}`. |
| Bearer token | Credential sent in the HTTP Authorization header. Possession grants its configured privileges. It is persisted locally but masked in the UI and journal. |
| GET | Read request. Reads a resource or collection; does not provision changes. |
| POST | Collection create request. This client uses it only for Users. The provider assigns the new resource's `id`. |
| PATCH / PatchOp | Partial update request with the SCIM PatchOp schema and `Operations` array. This client uses `replace` for User fields and `add`/filtered `remove` for Group membership. |
| PUT | Whole-resource replacement operation. Not used by this client. |
| DELETE | Resource deletion operation. Not used by this client; account removal is represented by deactivation. |
| Filter | SCIM expression narrowing a list or selecting values inside an attribute. Examples: `externalId eq "user-kaladin"` and `members[value eq "remote-id"]`. Values are quoted and escaped, and query strings are URL-encoded. |
| Pagination | Reading a collection in pages using 1-based `startIndex` and requested `count`. The provider may return fewer resources than requested. |
| ListResponse | SCIM list response with the ListResponse schema, `totalResults`, and `Resources`; pagination metadata includes `startIndex` and usually `itemsPerPage`. |
| Media type | `application/scim+json`, used for SCIM JSON request and response representation. |
| SCIM error | Error response, conventionally with the SCIM Error schema, `status`, optional `scimType`, and `detail`. Non-success HTTP responses fail the relevant operation and are retained in the journal. |
| Discovery | Reading `/ServiceProviderConfig`, `/Schemas`, and `/ResourceTypes` to learn capabilities and schemas. These endpoints are part of the domain, but this client does not currently call them. |
| ETag / `If-Match` | Resource version and conditional-request mechanisms for detecting concurrent changes. Not used by this client. |
| Bulk | SCIM batching of multiple operations into one request. Not implemented here; requests are individual. |

## Lifecycle and synchronization

| Term | Meaning |
| --- | --- |
| Provisioning | Creating or changing accounts and access on the service provider. Direction is client to provider. |
| Deprovisioning | Removing access. Here it means `active=false` plus removal of managed group memberships, not physical account deletion. |
| Reactivation | Setting `active=true` and restoring locally retained assignments on the next explicit sync. |
| Joiner / mover / leaver | Domain terms for account onboarding, changes of role or affiliation, and departure. Cross-company moves are not implemented as edits to an existing user's company. |
| Full sync | Explicit run that reads all remote Users and Groups with pagination, then reconciles all local desired state. It is not a destructive mirror and does not delete remote-only resources. |
| Incremental sync | Explicit run processing local changes not yet successfully checkpointed. Reads affected remote resources before writing. It is not a remote timestamp/delta query and does not detect arbitrary remote drift in unchanged resources. User work also triggers group membership reconciliation. |
| Aggregation | Fetching provider resources into a client's inventory or desired state as an import workflow. Not implemented. Full sync includes remote reads for reconciliation, but does not import remote-only data. |
| Reconciliation | Comparing desired and remote state, correlating resource IDs, and applying necessary differences within the client's ownership boundary. |
| Drift | A difference introduced remotely after an earlier sync. Full sync discovers and repairs drift for managed fields/memberships; incremental sync primarily responds to local edits. |
| Revision | Local change counter. Local mutations increment the relevant resource's `revision`. |
| Checkpoint | Last successfully reconciled local revision, stored in `synced_revision`. It advances only after successful resource processing, not simply because a run started. |
| Pending change | A resource whose revision does not match its checkpoint, including failed work. The UI preview is a local work list, not a promise of specific HTTP requests before remote state is read. |
| Sync run | One explicit full or incremental sync, recorded in `runs` with status and errors. A run may partially succeed. |
| Partial failure | Some resources succeed while others fail. Successful checkpoints remain; failed resources are pending for later manual retry. There is no distributed rollback. |
| Retry | Another attempt after failure. GET requests have bounded automatic retries. Writes are reconciled on a subsequent manual sync instead of being blindly repeated. |
| Uncertain outcome | A timeout or lost response after a write that may already have been applied. The next sync reads remote state to determine what remains necessary. |
| Idempotence | Repeating reconciliation against unchanged state causes no additional changes or duplicate resources. Provider uniqueness and stable correlation are essential to this behavior. |
| HTTP journal | Persistent per-attempt record of method, URL, selected/redacted headers, bodies, status, elapsed time, and run association. It contains synthetic identity data and must still be protected. |
| Seed / reset | Repeatable SQL initialization of local test data. Reset destroys local settings, history, and state but does not change the provider. |
