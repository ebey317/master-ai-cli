import unittest
from unittest.mock import patch, MagicMock
import subprocess
import time
from verifiers import VerifyResult, verify_process_running

class TestVerifyProcessRunning(unittest.TestCase):
    
    def test_empty_process_name(self):
        """Test that empty process name returns appropriate result"""
        result = verify_process_running("")
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertEqual(result.reason, "empty process name")
    
    @patch('master_ai.verifiers.subprocess.run')
    def test_process_found_immediately(self, mock_run):
        """Test that process found immediately returns success"""
        # Mock successful pgrep call
        mock_cp = MagicMock()
        mock_cp.returncode = 0
        mock_cp.stdout = "1234 /usr/bin/python3 -m master_ai\n"
        mock_cp.stderr = ""
        mock_run.return_value = mock_cp
        
        result = verify_process_running("master_ai", max_wait_s=0.1)
        
        self.assertTrue(result.ok)
        self.assertEqual(result.observed, "1234 /usr/bin/python3 -m master_ai")
        self.assertEqual(result.reason, "process 'master_ai' found")
        
    @patch('master_ai.verifiers.subprocess.run')
    def test_process_not_found(self, mock_run):
        """Test that process not found returns failure after timeout"""
        # Mock pgrep returning no matches (returncode 1)
        mock_cp = MagicMock()
        mock_cp.returncode = 1
        mock_cp.stdout = ""
        mock_cp.stderr = ""
        mock_run.return_value = mock_cp
        
        result = verify_process_running("nonexistent_process", max_wait_s=0.1)
        
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertIn("not found within 0.1s", result.reason)
        
    @patch('master_ai.verifiers.subprocess.run')
    def test_pgrep_not_available(self, mock_run):
        """Test that pgrep not available returns appropriate error"""
        # Mock FileNotFoundError when pgrep command is not found
        mock_run.side_effect = FileNotFoundError("pgrep not found")
        
        result = verify_process_running("some_process", max_wait_s=0.1)
        
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertEqual(result.reason, "pgrep not available on this system")
        
    @patch('master_ai.verifiers.subprocess.run')
    def test_pgrep_timeout(self, mock_run):
        """Test that pgrep timeout returns appropriate error"""
        # Mock subprocess.TimeoutExpired
        mock_run.side_effect = subprocess.TimeoutExpired("pgrep", 1.0)
        
        result = verify_process_running("some_process", max_wait_s=0.1)
        
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertIn("pgrep call exceeded", result.reason)
        
if __name__ == '__main__':
    unittest.main()