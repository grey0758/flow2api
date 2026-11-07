"""Native wire/geometry/output contracts; no provider requests or image changes."""
import hashlib,io,unittest
from src.services.original_aspect import (ORIGINAL_ASPECTS,NATIVE_ASPECT_ENUMS,
    reference_geometry,original_inputs,validate_output,DIMENSIONS,REFERENCE_ROLE)
from src.services.cookie_media import image_argument,ASPECTS
from src.services.flow_cookie_rpc import ProtocolError
from PIL import Image

def png(size=(800,800)):
    b=io.BytesIO();Image.new('RGB',size,'navy').save(b,format='PNG');return b.getvalue()

class NativeAspect(unittest.TestCase):
    def test_square_reference_and_all_explicit_target_enums(self):
        raw=png();before=hashlib.sha256(raw).hexdigest()
        for variant,(aspect,_,_)in ORIGINAL_ASPECTS.items():
            with self.subTest(variant=variant):
                trace=reference_geometry([raw],aspect)
                self.assertEqual(trace['mode'],'native_explicit_aspect')
                self.assertFalse(trace['reference_ratio_matches_target'])
                self.assertEqual(trace['sha256'],before)
                self.assertEqual(trace['additional_images'],0)
                self.assertTrue(trace['reference_bytes_unchanged'])
                refs=[{'name':'original-first'},{'name':'original-second'}]
                converted=original_inputs(refs)
                self.assertEqual(refs,[{'name':'original-first'},{'name':'original-second'}])
                self.assertEqual([r['imageInputType']for r in converted],[REFERENCE_ROLE]*2)
                arg=image_argument('project','prompt','GEM_PIX_2',aspect,'captcha','session',converted)
                one=arg[1][0]
                self.assertEqual(one[4],NATIVE_ASPECT_ENUMS[aspect])
                self.assertEqual(one[5],'GEM_PIX_2')
                self.assertEqual([r[0]for r in one[2]],['original-first','original-second'])
                self.assertEqual([r[4]for r in one[2]],[1,1])
    def test_existing_native_aspects_unchanged(self):
        for aspect,enum in {'IMAGE_ASPECT_RATIO_SQUARE':1,'IMAGE_ASPECT_RATIO_PORTRAIT':2,
                'IMAGE_ASPECT_RATIO_LANDSCAPE':3,'IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR':4,
                'IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE':5}.items():
            self.assertEqual(image_argument('p','p','GEM_PIX_2',aspect,'c','s')[1][0][4],enum)
    def test_invalid_and_missing_reference_stay_preaccount_failures(self):
        aspect=ORIGINAL_ASPECTS['four-five'][0]
        for refs in [[],[b'not an image']]:
            with self.assertRaises(ValueError):reference_geometry(refs,aspect)
        with self.assertRaises(ProtocolError):
            image_argument('p','p','GEM_PIX_2','invented','c','s')
    def test_output_size_validation_remains_strict_for_all_tiers(self):
        for tier,table in DIMENSIONS.items():
            for aspect,size in table.items():
                with self.subTest(tier=tier,aspect=aspect):
                    result=validate_output(png(size),aspect,tier)
                    self.assertEqual((result['width'],result['height']),size)
                    with self.assertRaisesRegex(ValueError,'output_dimensions_mismatch'):
                        validate_output(png(),aspect,tier)

if __name__=='__main__':unittest.main()
