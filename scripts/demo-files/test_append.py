

class WordCountWhitespaceTest(unittest.TestCase):
    def test_repeated_spaces_and_newlines(self):
        self.assertEqual(word_count("one  two\n\nthree "), 3)
        self.assertEqual(word_count("   "), 0)
