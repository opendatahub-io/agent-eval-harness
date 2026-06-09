"""Tests for S3 dataset download functionality."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, call
from agent_eval.evalhub.s3_dataset import download_dataset, DatasetInfo


class TestS3Dataset(unittest.TestCase):
    """Test cases for S3 dataset download."""

    def test_download_creates_case_dirs(self):
        """Test downloading dataset creates case directories with files."""
        # Mock S3 client
        s3_client = MagicMock()

        # Mock list_objects_v2 to return 2 cases with files
        s3_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "dataset/case-001/input.yaml"},
                {"Key": "dataset/case-001/annotations.yaml"},
                {"Key": "dataset/case-002/input.yaml"},
            ],
            "IsTruncated": False,
        }

        # Mock download_file to actually create the files
        def mock_download(bucket, key, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_text(f"mock content for {key}")

        s3_client.download_file.side_effect = mock_download

        # Create temp directory for test
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "cases"

            # Download dataset
            result = download_dataset(s3_client, "test-bucket", "dataset", dest)

            # Verify DatasetInfo
            self.assertEqual(result.num_cases, 2)
            self.assertEqual(result.case_ids, ["case-001", "case-002"])
            self.assertEqual(result.dest, dest)

            # Verify S3 client calls
            s3_client.list_objects_v2.assert_called_once_with(
                Bucket="test-bucket",
                Prefix="dataset/"
            )

            # Verify files were downloaded
            self.assertEqual(s3_client.download_file.call_count, 3)

            # Verify local files exist
            self.assertTrue((dest / "case-001" / "input.yaml").exists())
            self.assertTrue((dest / "case-001" / "annotations.yaml").exists())
            self.assertTrue((dest / "case-002" / "input.yaml").exists())

            # Verify file contents
            content = (dest / "case-001" / "input.yaml").read_text()
            self.assertIn("dataset/case-001/input.yaml", content)

    def test_download_empty_bucket(self):
        """Test downloading from empty bucket returns empty DatasetInfo."""
        # Mock S3 client
        s3_client = MagicMock()

        # Mock empty listing
        s3_client.list_objects_v2.return_value = {
            "IsTruncated": False,
        }

        # Create temp directory for test
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "cases"

            # Download dataset
            result = download_dataset(s3_client, "test-bucket", "dataset", dest)

            # Verify DatasetInfo
            self.assertEqual(result.num_cases, 0)
            self.assertEqual(result.case_ids, [])
            self.assertEqual(result.dest, dest)

            # Verify no downloads attempted
            s3_client.download_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
