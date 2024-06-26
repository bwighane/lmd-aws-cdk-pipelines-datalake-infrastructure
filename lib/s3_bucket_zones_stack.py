# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from aws_cdk import Stack, RemovalPolicy, CfnOutput, Duration
from constructs import Construct

from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deployment

import os


from .configuration import (
    PROD, S3_ACCESS_LOG_BUCKET, S3_CONFORMED_BUCKET, S3_KMS_KEY, S3_PURPOSE_BUILT_BUCKET, S3_RAW_BUCKET, TEST,
    get_environment_configuration, get_logical_id_prefix, get_resource_name_prefix,
)


class S3BucketZonesStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str,
        target_environment: str, deployment_account_id: str,
        **kwargs
    ) -> None:
        """
        CloudFormation stack to create AWS KMS Key, Amazon S3 resources such as buckets and bucket policies.

        @param scope Construct: Parent of this stack, usually an App or a Stage, but could be any construct.:
        @param construct_id str:
            The construct ID of this stack. If stackName is not explicitly defined,
            this id (and any parent IDs) will be used to determine the physical ID of the stack.
        @param target_environment str: The target environment for stacks in the deploy stage
        @param deployment_account_id: The id for the deployment account
        @param kwargs:
        """
        super().__init__(scope, construct_id, **kwargs)

        self.target_environment = target_environment
        mappings = get_environment_configuration(target_environment)
        logical_id_prefix = get_logical_id_prefix()
        resource_name_prefix = get_resource_name_prefix()
        self.removal_policy = RemovalPolicy.DESTROY
        if (target_environment == PROD or target_environment == TEST):
            self.removal_policy = RemovalPolicy.RETAIN

        s3_kms_key = self.create_kms_key(
            deployment_account_id,
            logical_id_prefix,
            resource_name_prefix,
        )
        access_logs_bucket = self.create_access_logs_bucket(
            f'{target_environment}{logical_id_prefix}AccessLogsBucket',
            f'{target_environment.lower()}-{resource_name_prefix}-{self.account}-{self.region}-access-logs',
            s3_kms_key,
        )
        raw_bucket = self.create_data_lake_zone_bucket(
            f'{target_environment}{logical_id_prefix}RawBucket',
            f'{target_environment.lower()}-{resource_name_prefix}-{self.account}-{self.region}-raw',
            access_logs_bucket,
            s3_kms_key,
        )

        s3_deployment.BucketDeployment(self, "InitialFolders",
                                       sources=[s3_deployment.Source.asset("./country-assets")],
                                       destination_bucket=raw_bucket
                                       )

        conformed_bucket = self.create_data_lake_zone_bucket(
            f'{target_environment}{logical_id_prefix}StagingBucket',
            f'{target_environment.lower()}-{resource_name_prefix}-{self.account}-{self.region}-staging',
            access_logs_bucket,
            s3_kms_key,
        )
        purpose_built_bucket = self.create_data_lake_zone_bucket(
            f'{target_environment}{logical_id_prefix}CuratedBucket',
            f'{target_environment.lower()}-{resource_name_prefix}-{self.account}-{self.region}-curated',
            access_logs_bucket,
            s3_kms_key,
        )

        # Stack Outputs that are programmatically synchronized
        CfnOutput(
            self,
            f'{target_environment}{logical_id_prefix}KmsKeyArn',
            value=s3_kms_key.key_arn,
            export_name=mappings[S3_KMS_KEY]
        )
        CfnOutput(
            self,
            f'{target_environment}{logical_id_prefix}AccessLogsBucketName',
            value=access_logs_bucket.bucket_name,
            export_name=mappings[S3_ACCESS_LOG_BUCKET]
        )
        CfnOutput(
            self,
            f'{target_environment}{logical_id_prefix}RawBucketName',
            value=raw_bucket.bucket_name,
            export_name=mappings[S3_RAW_BUCKET]
        )
        CfnOutput(
            self,
            f'{target_environment}{logical_id_prefix}StagingBucketName',
            value=conformed_bucket.bucket_name,
            export_name=mappings[S3_CONFORMED_BUCKET]
        )
        CfnOutput(
            self,
            f'{target_environment}{logical_id_prefix}CuratedBucketName',
            value=purpose_built_bucket.bucket_name,
            export_name=mappings[S3_PURPOSE_BUILT_BUCKET]
        )

    def create_kms_key(self, deployment_account_id, logical_id_prefix, resource_name_prefix) -> kms.Key:
        """
        Creates an AWS KMS Key and attaches a Key policy

        @param deployment_account_id: The id for the deployment account
        @param logical_id str: The logical id prefix to apply to all CloudFormation resources
        @param resource_name_prefix: The resource name prefix to apply to all resource names
        """
        s3_kms_key = kms.Key(
            self,
            f'{self.target_environment}{logical_id_prefix}KmsKey',
            admins=[iam.AccountPrincipal(self.account)],  # Gives account users admin access to the key
            description='Key used for encrypting Data Lake S3 Buckets',
            removal_policy=self.removal_policy,
            alias=f'{self.target_environment.lower()}-{resource_name_prefix}-kms-key'
        )
        # Gives account users and deployment account users access to use the key
        s3_kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[
                    iam.AccountPrincipal(self.account),
                    iam.AccountPrincipal(deployment_account_id),
                ],
                actions=[
                    'kms:Encrypt',
                    'kms:Decrypt',
                    'kms:ReEncrypt*',
                    'kms:GenerateDataKey*',
                    'kms:DescribeKey',
                ],
                resources=["*"],
            )
        )

        return s3_kms_key

    def create_data_lake_zone_bucket(self, logical_id, bucket_name, access_logs_bucket, s3_kms_key) -> s3.Bucket:
        """
        Creates an Amazon S3 bucket and attaches bucket policy with necessary guardrails.
        It enables server-side encryption using provided KMS key and leverage S3 bucket key feature.

        @param logical_id str: The logical id to apply to the bucket
        @param bucket_name str: The name for the bucket resource
        @param access_logs_bucket s3.Bucket: The bucket to target for Access Logging
        @param s3_kms_key kms.Key: The KMS Key to use for encryption of data at rest

        @return: s3.Bucket: The bucket that was created
        """
        lifecycle_rules = [
            s3.LifecycleRule(
                enabled=True,
                expiration=Duration.days(60),
                noncurrent_version_expiration=Duration.days(30),
            )
        ]
        if self.target_environment == PROD:
            lifecycle_rules = [
                s3.LifecycleRule(
                    enabled=True,
                    expiration=Duration.days(2555),
                    noncurrent_version_expiration=Duration.days(90),
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(365),
                        )
                    ]
                )
            ]
        bucket = s3.Bucket(
            self,
            id=logical_id,
            access_control=s3.BucketAccessControl.PRIVATE,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            bucket_key_enabled=True,
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=s3_kms_key,
            lifecycle_rules=lifecycle_rules,
            public_read_access=False,
            removal_policy=self.removal_policy,
            versioned=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            server_access_logs_bucket=access_logs_bucket,
            server_access_logs_prefix=bucket_name,
        )
        policy_document_statements = [
            iam.PolicyStatement(
                sid='OnlyAllowSecureTransport',
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=[
                    's3:GetObject',
                    's3:PutObject',
                ],
                resources=[f'{bucket.bucket_arn}/*'],
                conditions={'Bool': {'aws:SecureTransport': 'false'}}
            )
        ]
        # Prevents user deletion of buckets
        if self.target_environment == PROD or self.target_environment == TEST:
            policy_document_statements.append(
                iam.PolicyStatement(
                    sid='BlockUserDeletionOfBucket',
                    effect=iam.Effect.DENY,
                    principals=[iam.AnyPrincipal()],
                    actions=[
                        's3:DeleteBucket',
                    ],
                    resources=[bucket.bucket_arn],
                    conditions={'StringLike': {'aws:userId': f'arn:aws:iam::{self.account}:user/*'}}
                )
            )
        for statement in policy_document_statements:
            bucket.add_to_resource_policy(statement)

        return bucket

    def create_access_logs_bucket(self, logical_id, bucket_name, s3_kms_key) -> s3.Bucket:
        """
        Creates an Amazon S3 bucket to store S3 server access logs. It attaches bucket policy with necessary guardrails.
        It enables server-side encryption using provided KMS key and leverage S3 bucket key feature.

        @param logical_id str: The logical id to apply to the bucket
        @param bucket_name str: The name for the bucket resource
        @param s3_kms_key kms.Key: The KMS Key to use for encryption of data at rest

        @return: The bucket that was created
        """
        return s3.Bucket(
            self,
            id=logical_id,
            access_control=s3.BucketAccessControl.LOG_DELIVERY_WRITE,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            bucket_key_enabled=True,
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=s3_kms_key,
            public_read_access=False,
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
        )
