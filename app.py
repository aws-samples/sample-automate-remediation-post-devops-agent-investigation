#!/usr/bin/env python3
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from devops_remediation.devops_remediation_stack import DevOpsRemediationStack

app = cdk.App()
DevOpsRemediationStack(app, "DevOpsRemediationStack")

# Run the cdk-nag AWS Solutions rule pack against the app during synthesis.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
