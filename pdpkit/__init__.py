"""pdpkit: clone a competitor product page into our own Shopify PDP.

Pipeline (python pdp.py <command>):
  grab      <url>                 competitor images -> competitor_imgs/, product_summary.md/.json
  generate  <slug> --prompt ...   Higgsfield images from those references -> <name>_shopify_PDP_imgs/
  upload    <slug>                generated images -> Shopify product (draft)
  guide     <slug> --template ... PDF instructions for Fudge from template + product_summary
  run       <url> ...             all of the above in order
"""
