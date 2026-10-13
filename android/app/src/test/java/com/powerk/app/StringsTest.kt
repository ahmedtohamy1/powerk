package com.powerk.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

/**
 * Guards the translated string files: every locale must keep the same format arguments,
 * no stray '%' that would throw at format time, and the Arabic file must really be Arabic
 * (a file saved in the wrong encoding shows up here instead of on the phone).
 */
class StringsTest {

    private val format = Regex("""%(\d+)\$([a-zA-Z])""")
    private val default = load("src/main/res/values/strings.xml")
    private val arabic = load("src/main/res/values-ar/strings.xml")

    private fun load(path: String): Map<String, String> {
        val file = File(path)
        assertTrue("missing $path (unit tests run from the app module dir)", file.isFile)
        val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(file)
        val nodes = doc.getElementsByTagName("string")
        return (0 until nodes.length).associate { i ->
            val element = nodes.item(i) as Element
            element.getAttribute("name") to (element.textContent ?: "")
        }
    }

    @Test
    fun everyValueIsTranslated() {
        assertTrue("default strings are empty", default.isNotEmpty())
        val missing = default.keys - arabic.keys
        assertEquals("strings missing from values-ar", emptySet<String>(), missing)
    }

    @Test
    fun everyLocaleUsesTheSameFormatArguments() {
        default.forEach { (name, english) ->
            val translated = arabic[name] ?: return@forEach
            val expected = format.findAll(english).map { it.value }.sorted().toList()
            val actual = format.findAll(translated).map { it.value }.sorted().toList()
            assertEquals("format arguments differ for '$name'", expected, actual)
        }
    }

    @Test
    fun noStrayPercentSigns() {
        (default + arabic).forEach { (name, text) ->
            // %% is a literal percent in String.format, not an unformatted argument.
            val stray = strayPercentCount(text)
            assertEquals("unformatted % in '$name'", 0, stray)
        }
    }

    private fun strayPercentCount(text: String) = Regex("""%(?!\d+\$)""").findAll(text.replace("%%", "")).count()

    @Test fun literalPercentEscapesAreAllowedButStraySignsStillFail() {
        assertEquals(0, strayPercentCount("%1\$d%% of budget"))
        assertEquals(1, strayPercentCount("%1\$d% of budget"))
        assertEquals(1, strayPercentCount("%%%"))
    }

    @Test
    fun arabicStringsAreReallyArabic() {
        assertTrue(
            "values-ar contains no Arabic letters (wrong encoding?)",
            arabic.values.any { value -> value.any { it in '\u0600'..'\u06FF' } },
        )
    }
}
