#!/usr/bin/env python3
"""Run isolated S3 pagination regressions; credentials come only from the environment."""
import hashlib
import json
import os
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

endpoint, mode = sys.argv[1:3]
part_size = 5 * 1024 * 1024
s3 = boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                  config=Config(s3={'addressing_style': 'path'},
                                retries={'max_attempts': 12, 'mode': 'standard'}))
bucket = 'multipart-test-' + uuid.uuid4().hex[:12]
s3.create_bucket(Bucket=bucket)
results = {'mode': mode, 'endpoint': endpoint, 'part_size': part_size, 'cases': []}

def payload(number):
    return bytes([number % 251]) * part_size

def exercise(count, expect_failure=False):
    key = f'parts-{count}'
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)['UploadId']
    parts = []
    expected_hash = hashlib.sha256()
    started = time.monotonic()
    completed = False
    try:
        for number in range(1, count+1):
            data = payload(number)
            expected_hash.update(data)
            response = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload,
                                      PartNumber=number, Body=data)
            parts.append({'ETag': response['ETag'], 'PartNumber': number})
            if number % 100 == 0:
                print(json.dumps({'case': count, 'uploaded': number}), flush=True)
        try:
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload,
                                         MultipartUpload={'Parts': parts})
            completed = True
        except ClientError as error:
            code = error.response['Error']['Code']
            if not expect_failure or code != 'InvalidPart':
                raise
            case = {'parts': count, 'expected_failure': code, 'uploaded_parts': len(parts)}
            results['cases'].append(case)
            print(json.dumps(case), flush=True)
            return
        if expect_failure:
            raise AssertionError('Original production image unexpectedly completed >1000 parts')
        size = s3.head_object(Bucket=bucket, Key=key)['ContentLength']
        assert size == count * part_size, (size, count * part_size)
        actual_hash = hashlib.sha256()
        body = s3.get_object(Bucket=bucket, Key=key)['Body']
        for chunk in body.iter_chunks(chunk_size=1024*1024):
            actual_hash.update(chunk)
        body.close()
        assert actual_hash.digest() == expected_hash.digest(), 'Streaming SHA256 mismatch'
        boundary = min(1000, count-1)
        start, end = boundary * part_size - 16, boundary * part_size + 15
        ranged = s3.get_object(Bucket=bucket, Key=key, Range=f'bytes={start}-{end}')
        data = ranged['Body'].read()
        expected_range = bytes([boundary % 251])*16 + bytes([(boundary+1) % 251])*16
        assert data == expected_range, 'Range across part boundary mismatch'
        assert ranged['ContentRange'] == f'bytes {start}-{end}/{size}'
        assert not s3.list_multipart_uploads(Bucket=bucket).get('Uploads'), 'Completed upload leaked'
        case = {'parts': count, 'bytes': size, 'sha256': actual_hash.hexdigest(),
                'range_boundary_part': boundary, 'range_ok': True,
                'elapsed_seconds': round(time.monotonic()-started, 2)}
        results['cases'].append(case)
        print(json.dumps(case), flush=True)
    finally:
        if not completed:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
        else:
            s3.delete_object(Bucket=bucket, Key=key)

try:
    for count in ([1001] if mode == 'original' else [999, 1000, 1001]):
        exercise(count, expect_failure=mode == 'original')
    key = 'abort-cleanup'
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key)['UploadId']
    s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=payload(1))
    s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
    assert not s3.list_multipart_uploads(Bucket=bucket).get('Uploads'), 'Abort leaked upload'
    try:
        s3.list_parts(Bucket=bucket, Key=key, UploadId=upload)
        raise AssertionError('Aborted upload still accessible')
    except ClientError as error:
        # POSIX currently reports NoSuchKey here; both denote absent state.
        assert error.response['ResponseMetadata']['HTTPStatusCode'] == 404
        assert error.response['Error']['Code'] in {'NoSuchUpload', 'NoSuchKey'}
    results['abort_cleanup'] = True
    print('RESULT ' + json.dumps(results), flush=True)
finally:
    s3.delete_bucket(Bucket=bucket)
