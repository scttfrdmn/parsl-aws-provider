# Security Hardening Implementation Plan
## Parsl Ephemeral AWS Provider

### Phase 1.1: Network Security Framework (Days 1-4)

#### Task 1.1.1: Create Security Configuration System
**Files to Create:**
- `parsl_ephemeral_aws/security/network_policy.py` - Network security policy engine
- `parsl_ephemeral_aws/security/cidr_manager.py` - CIDR block validation and management
- `parsl_ephemeral_aws/config/security_config.py` - Security configuration schema

**Implementation Steps:**
1. Replace hardcoded 0.0.0.0/0 CIDR blocks with configurable security policies
2. Implement network security validation framework
3. Add environment-based security profiles (dev/staging/prod)
4. Create security policy templates for common deployment patterns

**Key Changes:**
```python
# NEW: Security policy framework
@dataclass
class NetworkSecurityPolicy:
    admin_cidr_blocks: List[str] = field(default_factory=lambda: ["10.0.0.0/8"])
    ssh_allowed_cidrs: List[str] = field(default_factory=lambda: [])
    parsl_communication_cidrs: List[str] = field(default_factory=lambda: [])
    public_access_ports: List[int] = field(default_factory=list)

    def validate_cidr_blocks(self) -> bool:
        # Prevent 0.0.0.0/0 in production
        # Validate CIDR format
        # Check for overly permissive rules
```

**Files to Modify:**
1. `parsl_ephemeral_aws/constants.py` - Replace DEFAULT_INBOUND_RULES
2. `parsl_ephemeral_aws/network/security.py` - Add policy validation
3. `parsl_ephemeral_aws/compute/*.py` - Update all security group creations
4. `parsl_ephemeral_aws/modes/*.py` - Update route table configurations
5. `parsl_ephemeral_aws/templates/` - Update all infrastructure templates

**Success Criteria:**
- Zero instances of 0.0.0.0/0 in production configuration
- All security group rules validated against policy
- Network security tests passing (100% coverage for security rules)

#### Task 1.1.2: Implement Network Segmentation
**Timeline: Day 2-3**

Create proper network isolation:
```python
# NEW: Network segmentation
class NetworkSegmentation:
    def create_compute_subnet(self, vpc_id: str) -> str:
        # Isolated subnet for compute workloads
        # No direct internet access

    def create_management_subnet(self, vpc_id: str) -> str:
        # Separate subnet for bastion/management
        # Restricted access

    def create_nat_gateway(self) -> str:
        # Controlled outbound internet access
```

#### Task 1.1.3: Security Group Template System
**Timeline: Day 3-4**

Replace hardcoded rules with templates:
```python
# NEW: Security group templates
class SecurityGroupTemplates:
    @staticmethod
    def compute_worker_sg(vpc_cidr: str) -> Dict:
        return {
            "GroupDescription": "Parsl compute workers - restricted access",
            "VpcId": vpc_id,
            "SecurityGroupRules": [
                # Only allow VPC internal communication
                {"IpProtocol": "tcp", "FromPort": 54000, "ToPort": 55000,
                 "CidrBlocks": [vpc_cidr]},
            ]
        }
```
